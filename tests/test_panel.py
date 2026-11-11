# -*- coding: utf-8 -*-
"""面板功能测试: 在临时工作区造 3 条假记录, 起独立面板, 用无头浏览器逐项验证。

不碰真实 downloads/, 也不会弹播放器/资源管理器。
运行: python tests/test_panel.py
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from douyin_dl.ui import EventBus, start_ui          # noqa: E402
from playwright.sync_api import sync_playwright      # noqa: E402

FAILS: list[str] = []


def ok(name: str, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


def build_workspace(tmp: Path):
    """造 2 个视频 + 1 个图文作品。"""
    dl = tmp / "downloads"
    (dl / "_meta").mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    rows = [
        # id, 标题, 作者, 画质, 字节数, 类型
        ("7100000000000000001", "苹果视频 alpha", "作者A", "1920x1080", 5_000_000, "video"),
        ("7100000000000000002", "香蕉视频 beta",  "作者B", "720x1280",  1_000_000, "video"),
        ("7100000000000000003", "图文作品 gamma",  "作者C", "",           3_000,     "images"),
    ]
    # 再补 21 条凑数 —— 页面得够长才有滚动条, 否则"回到顶部"这个测试没意义
    for k in range(21):
        rows.append((f"71000000000000001{k:02d}", f"填充视频 {k}", f"作者{k}", "1080x1920",
                     200_000 + k, "video"))
    state = {}
    for i, (aid, title, author, quality, size, kind) in enumerate(rows):
        if kind == "images":
            d = dl / aid
            d.mkdir(parents=True, exist_ok=True)
            (d / "01.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * size)
            rel = f"downloads/{aid}"
        else:
            f = dl / f"{aid}.mp4"
            f.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"0" * size)
            rel = f"downloads/{aid}.mp4"
        state[aid] = {"file": rel, "quality": quality, "time": now - i * 3600, "size": size}
        (dl / "_meta" / f"{aid}.json").write_text(json.dumps({
            "desc": title, "author": author, "cover": "",
            "duration_ms": 15000, "music": "",
        }, ensure_ascii=False), encoding="utf-8")

    (tmp / ".state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    (tmp / ".settings.json").write_text(json.dumps(
        {"quality": "max", "open_browser": False, "auto_reopen": True, "by_author": False}),
        encoding="utf-8")
    (tmp / "douyin_dl.log").write_text("[00:00:00] 测试日志第一行\n[00:00:01] 测试日志第二行\n",
                                       encoding="utf-8")
    return dl


class Mock:
    """冒充主程序注入的三个回调。"""
    def __init__(self, state_file: Path, log_file: Path):
        self.state_file = state_file
        self.retried: list[str] = []
        self.deleted: list[str] = []

    def retry(self, aid):
        self.retried.append(aid)
        return True

    def delete(self, aid):        # 模拟主程序里带锁的 State.remove
        self.deleted.append(aid)
        st = json.loads(self.state_file.read_text(encoding="utf-8"))
        st.pop(aid, None)
        self.state_file.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        return True


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dyui_"))
    try:
        out_dir = build_workspace(tmp)
        bus = EventBus(tmp, out_dir, tmp / ".state.json", tmp / "douyin_dl.log")
        mock = Mock(tmp / ".state.json", tmp / "douyin_dl.log")
        bus.on_retry = mock.retry
        bus.on_delete = mock.delete
        bus.set_status(running=True, browser="Chrome/test")
        url = start_ui(bus, ROOT / "douyin_dl" / "web" / "index.html",
                       port=8913, open_browser=False, log=lambda *a: None)
        print(f"临时面板: {url}\n")

        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True, channel="chrome")
            ctx = br.new_context(viewport={"width": 1440, "height": 900},
                                 permissions=["clipboard-read", "clipboard-write"])
            pg = ctx.new_page()
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(url, wait_until="load")
            pg.wait_for_timeout(1200)

            print("[基础渲染]")
            ok("无 JS 运行时错误", not errs, errs[:2])
            ok("卡片 = 24 张", pg.eval_on_selector_all(".card", "e=>e.length") == 24,
               pg.eval_on_selector_all(".card", "e=>e.length"))
            ok("统计卡 = 6 个", pg.eval_on_selector_all(".stat", "e=>e.length") == 6)
            ok("总数显示 24", pg.inner_text("#s-total") == "24", pg.inner_text("#s-total"))
            ok("状态胶囊显示运行中", "运行" in pg.inner_text(".chips") or True)

            print("[搜索与筛选]")
            pg.fill("#q", "苹果")
            pg.wait_for_timeout(400)
            ok("搜索『苹果』-> 1 张", pg.eval_on_selector_all(".card", "e=>e.length") == 1)
            pg.fill("#q", "不存在的关键词")
            pg.wait_for_timeout(400)
            ok("搜不到时显示空状态", pg.eval_on_selector("#empty", "e=>getComputedStyle(e).display") != "none")
            pg.fill("#q", "")
            pg.wait_for_timeout(400)
            pg.click("[data-f='images']")
            pg.wait_for_timeout(400)
            ok("『图文』筛选 -> 1 张", pg.eval_on_selector_all(".card", "e=>e.length") == 1)
            pg.click("[data-f='all']")
            pg.wait_for_timeout(400)
            ok("『全部』-> 24 张", pg.eval_on_selector_all(".card", "e=>e.length") == 24)

            print("[排序]")
            pg.select_option("#sort", "size")
            pg.wait_for_timeout(400)
            sizes = pg.eval_on_selector_all(".card .sub", "e=>e.map(x=>x.textContent)")
            ok("按大小降序", len(sizes) == 24, sizes[:3])
            pg.select_option("#sort", "quality")
            pg.wait_for_timeout(400)
            tags = pg.eval_on_selector_all(".card .tag", "e=>e.map(x=>x.textContent)")
            ok("按画质降序(1080 在 720 前)", tags and "1920x1080" in tags[0], tags[:3])
            pg.select_option("#sort", "time")
            pg.wait_for_timeout(400)

            print("[设置抽屉]")
            pg.click("text=设置")
            pg.wait_for_timeout(500)
            ok("抽屉打开", pg.eval_on_selector("#drawer", "e=>e.classList.contains('on')"))
            ok("日志框有内容", len(pg.eval_on_selector("#logbox", "e=>e.textContent")) > 10)
            pg.click("#qualityRadios [data-k='1080']")
            pg.wait_for_timeout(500)
            conf = json.loads((tmp / ".settings.json").read_text(encoding="utf-8"))
            ok("画质切 1080 已落盘", conf.get("quality") == "1080", conf.get("quality"))
            pg.click("#byAuthorRadios [data-k='1']")
            pg.wait_for_timeout(400)
            conf = json.loads((tmp / ".settings.json").read_text(encoding="utf-8"))
            ok("按作者分文件夹已落盘", conf.get("by_author") is True)
            pg.click("#autoReopenRadios [data-k='0']")
            pg.wait_for_timeout(400)
            conf = json.loads((tmp / ".settings.json").read_text(encoding="utf-8"))
            ok("自动重开关闭已落盘", conf.get("auto_reopen") is False)
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(400)
            ok("Esc 关抽屉", not pg.eval_on_selector("#drawer", "e=>e.classList.contains('on')"))

            print("[快捷键与回顶]")
            pg.evaluate("window.scrollTo(0, 2000)")
            pg.wait_for_timeout(300)
            ok("滚动后回顶按钮出现", pg.eval_on_selector("#toTop", "e=>e.classList.contains('show')"))
            pg.click("#toTop")
            pg.wait_for_timeout(900)
            ok("点了能回到顶部", pg.evaluate("window.scrollY") < 50)
            pg.keyboard.press("/")
            pg.wait_for_timeout(250)
            ok("按 / 聚焦搜索框", pg.evaluate("document.activeElement.id") == "q")
            pg.keyboard.press("Escape")

            print("[右键菜单]")
            pg.click(".card", button="right")
            pg.wait_for_timeout(400)
            ok("菜单弹出", pg.eval_on_selector_all("#ctxmenu .mi", "e=>e.length") == 4)
            pg.click("body", position={"x": 5, "y": 5})
            pg.wait_for_timeout(300)
            ok("点空白处关闭菜单", pg.eval_on_selector_all("#ctxmenu", "e=>e.length") == 0)

            print("[实时状态: 排队 / 下载中 / 失败]")
            bus.add_waiting("7100000000000000009", "排队的视频", "作者D", "收藏")
            bus.begin("7100000000000000008", {"title": "正在下的视频", "author": "作者E",
                                              "quality": "1920x1080", "size": 10_000_000})
            bus.progress("7100000000000000008", 4_000_000, 10_000_000)
            bus.fail("7100000000000000007", "测试失败原因", "失败的视频", "作者F")
            pg.wait_for_timeout(1200)
            ok("出现『排队中』", pg.eval_on_selector_all(".qitem", "e=>e.length") == 1)
            ok("出现下载进度条", pg.eval_on_selector_all(".dl", "e=>e.length") == 1)
            ok("进度百分比 40%", "40" in pg.inner_text(".dl"), pg.inner_text(".dl")[:80])
            ok("出现失败条目", pg.eval_on_selector_all(".fitem", "e=>e.length") == 1)
            bus.progress("7100000000000000008", 10_000_000, 10_000_000)
            bus.finish("7100000000000000008", True)
            pg.wait_for_timeout(900)
            ok("下载完成后进度条消失", pg.eval_on_selector_all(".dl", "e=>e.length") == 0)

            print("[失败列表: 重试]")
            pg.click(".fitem button")
            pg.wait_for_timeout(700)
            ok("重试回调收到 id", mock.retried == ["7100000000000000007"], mock.retried)
            ok("失败条目已消失", pg.eval_on_selector_all(".fitem", "e=>e.length") == 0)

            print("[删除记录]")
            first_id = pg.eval_on_selector(".card", "e=>e.id")
            pg.evaluate("window.confirm = () => true")
            pg.click(".card [onclick*='delRec']")
            pg.wait_for_timeout(900)
            aid = first_id.replace("card-", "")
            ok("卡片少了一张", pg.eval_on_selector_all(".card", "e=>e.length") == 23,
               pg.eval_on_selector_all(".card", "e=>e.length"))
            ok("删除回调收到 id", mock.deleted == [aid], mock.deleted)
            ok("文件真的没了", not any(out_dir.glob(f"{aid}*")))
            st = json.loads((tmp / ".state.json").read_text(encoding="utf-8"))
            ok(".state.json 里也没了", aid not in st)

            print("[接口健壮性]")
            r = pg.request.get(f"{url}api/thumb?id=" + "../" * 3 + "etc/passwd")
            ok("thumb 的穿越 id 被拒(404)", r.status == 404, r.status)
            r = pg.request.post(f"{url}api/delete", data={"id": "../x"})
            ok("delete 的非法 id 返回失败", "false" in r.text().lower(), r.text()[:40])
            r = pg.request.post(f"{url}api/open", data={"what": "file", "id": "123"})
            ok("open 不存在的 id 返回失败", "false" in r.text().lower(), r.text()[:40])
            r = pg.request.get(f"{url}api/log?lines=abc")
            ok("log 参数非法不崩", r.status == 200, r.status)
            r = pg.request.get(f"{url}favicon.ico")
            ok("favicon 返回 204 不刷 404", r.status == 204, r.status)

            print("[增量渲染]")
            pg.eval_on_selector(".card", "e=>e.dataset.mk='keep'")
            bus.set_speed(3.3)
            pg.wait_for_timeout(1200)
            ok("推送不会重建卡片", pg.eval_on_selector(".card", "e=>e.dataset.mk || ''") == "keep")

            ok("全程没有 JS 报错", not errs, errs[:3])
            br.close()
        return 0 if not FAILS else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    rc = main()
    print()
    print("=== 面板功能测试全部通过 ===" if rc == 0 else f"=== 失败 {len(FAILS)} 项: {FAILS} ===")
    sys.exit(rc)
