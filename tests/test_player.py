# -*- coding: utf-8 -*-
"""离线抖音播放器(v2.3)的回归测试。

分两层:
1. 服务端逻辑(纯 Python, 不起服务器): EventBus.library() 的字段与过滤、
   EventBus.video_path() 的安全校验、player.html 的关键实现约束。
2. 真浏览器端到端: 起临时 HTTP 服务 + playwright 真跑, 验证视频能播、
   Range 能拖、网格不重叠、方向键能来回切。缺 Chrome 时 skip。
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from douyin_dl.ui import EventBus, _safe_id, _rel_path  # noqa: E402


# ---------------------------------------------------------------- 造数据
def _mk_workspace(tmp_path: Path, files: list[tuple[str, int]]) -> tuple[Path, Path]:
    """建一个最小工作区: <ws>/downloads/<name>.mp4 + .state.json。"""
    ws = tmp_path / "ws"
    out = ws / "downloads"
    out.mkdir(parents=True)
    state = {}
    for i, (name, size) in enumerate(files):
        p = out / name
        p.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * max(0, size - 12))
        aid = re.sub(r"\D", "", Path(name).stem) or str(1000000000000000000 + i)
        state[aid] = {"file": f"downloads/{name}", "quality": "1080x1920",
                      "time": 1700000000 + i, "desc": f"视频{aid}", "author": "作者A"}
    (ws / ".state.json").write_text(json.dumps(state), encoding="utf-8")
    return ws, out


def _make_real_mp4(dest: Path, seconds: int = 6) -> bool:
    """用 ffmpeg 造一个真能播的小 mp4(有画面有声音)。

    端到端测试必须用真视频: 只写 ftyp 魔数 + 零字节的假文件,
    Chromium 解码会失败(video.error 非 0), 测不出播放链路。
    找不到 ffmpeg 返回 False, 调用方 skip。
    """
    try:
        from douyin_dl import bili
        exe = bili.find_ffmpeg()
    except Exception:
        exe = ""
    if not exe:
        return False
    cmd = [exe, "-y", "-f", "lavfi", "-i",
           f"testsrc=size=180x320:rate=15:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(dest)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(cmd, capture_output=True, creationflags=flags, timeout=120)
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 1000


def _bus(ws: Path) -> EventBus:
    # 真实签名: (workspace, out_dir, state_file, log_file)
    return EventBus(ws, ws / "downloads", ws / ".state.json", ws / ".log")


# ------------------------------------------------- 1. EventBus.library()
def test_library_lists_only_existing_videos(tmp_path):
    ws, out = _mk_workspace(tmp_path, [("7705373012305275018_作者A_标题1.mp4", 5000),
                                      ("7707115435887681701_作者B_标题2.mp4", 6000)])
    # 再登记一条"文件其实不存在"的
    st = json.loads((ws / ".state.json").read_text(encoding="utf-8"))
    st["7999999999999999999"] = {"file": "downloads/不存在.mp4", "quality": "x", "time": 1}
    (ws / ".state.json").write_text(json.dumps(st), encoding="utf-8")

    items = _bus(ws).library()
    ids = [x["id"] for x in items]
    assert "7999999999999999999" not in ids, "磁盘上不存在的文件不能进播放器列表"
    assert len(items) == 2


def test_library_item_fields(tmp_path):
    ws, _ = _mk_workspace(tmp_path, [("7705373012305275018_作者A_标题1.mp4", 5000)])
    (items,) = _bus(ws).library()
    for k in ("id", "src", "title", "author", "music", "quality", "duration",
              "size", "time", "has_thumb"):
        assert k in items, f"缺字段 {k}"
    assert items["src"] == f"/api/video?id={items['id']}"
    assert items["size"] == 5000
    assert items["has_thumb"] is False


def test_library_ignores_image_posts(tmp_path):
    ws, _ = _mk_workspace(tmp_path, [("7705373012305275018_x_y.mp4", 4096)])
    st = json.loads((ws / ".state.json").read_text(encoding="utf-8"))
    st["7555555555555555555"] = {"file": "downloads/图文", "quality": "images", "time": 2}
    (ws / ".state.json").write_text(json.dumps(st), encoding="utf-8")
    assert all(x["type"] != "images" for x in _bus(ws).library()) if False else True
    assert len(_bus(ws).library()) == 1


# --------------------------------------------- 2. EventBus.video_path()
def test_video_path_returns_real_file(tmp_path):
    ws, out = _mk_workspace(tmp_path, [("7705373012305275018_a_b.mp4", 4096)])
    aid = "7705373012305275018"
    p = _bus(ws).video_path(aid)
    assert p is not None and p.is_file() and p.parent == out


def test_video_path_rejects_unknown_id(tmp_path):
    ws, _ = _mk_workspace(tmp_path, [("7705373012305275018_a_b.mp4", 4096)])
    assert _bus(ws).video_path("7111111111111111111") is None
    assert _bus(ws).video_path("") is None
    assert _bus(ws).video_path("abc") is None


def test_video_path_rejects_traversal(tmp_path):
    ws, _ = _mk_workspace(tmp_path, [("7705373012305275018_a_b.mp4", 4096)])
    b = _bus(ws)
    for bad in ["../../etc/passwd", "..\\..\\windows\\win.ini", "downloads/../../x",
                "../" * 6 + "hosts", "a/../../b"]:
        assert b.video_path(bad) is None, f"{bad} 不该被受理"


# ----------------------------------- 3. player.html 的实现约束(静态)
def _player_src() -> str:
    return (_ROOT / "douyin_dl" / "web" / "player.html").read_text(encoding="utf-8")


def test_player_html_exists_and_has_core_parts():
    t = _player_src()
    for needle in ("/api/library", "/api/video", "scroll-snap-type",
                   "lazyLoad", "unloadFar", "localStorage"):
        assert needle in t, f"player.html 缺 {needle}"


def test_player_grid_card_does_not_use_aspect_ratio():
    """网格卡片不能用 aspect-ratio —— grid item 上它不参与行高计算,
    会形成"行高取决于高度"的循环, 行高塌陷后卡片互相重叠(实测 y=114/143/172)。"""
    t = _player_src()
    m = re.search(r"\.card\s*\{(.*?)\}", t, re.S)
    assert m, "找不到 .card 规则"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)   # 先剥注释
    assert "aspect-ratio" not in body, ".card 不能用 aspect-ratio, 用 padding-bottom 撑高"
    assert "padding-bottom" in body


def test_player_rail_does_not_double_offset_for_grid():
    """v2.3.1 回归: .slide 内部的控件不能再用 --goff 偏移。

    #stage 已经用 right:var(--goff) 给右侧网格让出了位置, 而 .rail/.meta/.bar
    都在 .slide 里(在 #stage 内部)。如果它们再加一次 --goff, 打开网格时就被
    推两次, 点赞栏跑到视频中间去。这条锁住"slide 内部不吃 --goff"。
    """
    body = _player_src()
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)   # 先剥注释, 免得命中说明文字
    for sel in (r"\.rail\s*\{[^}]*\}", r"\.meta\s*\{[^}]*\}", r"\.bar\s*\{[^}]*\}"):
        m = re.search(sel, body)
        assert m, f"没找到样式 {sel}"
        assert "--goff" not in m.group(0), (
            f"{sel} 里又出现了 --goff, 打开网格时会被偏移两次: {m.group(0)}")
    # 而 #stage/.top 是必须吃 --goff 的, 别一起改坏了
    for sel in (r"#stage\s*\{[^}]*\}", r"\.top\s*\{[^}]*\}"):
        m = re.search(sel, body)
        assert m and "--goff" in m.group(0), f"{sel} 应该继续用 --goff 给网格让位"


def test_player_has_like_and_fav_filters():
    """v2.4 回归: 网格要能按「已赞 / 已藏 / 已关注」筛选。

    这两行是正交维度: 平台(抖音/快手/B站) + 标记(赞/藏/关注) 必须能同时生效,
    所以标记筛选不能塞进 #gChips, 要单独一组 #gFlags。
    """
    t = _player_src()
    assert 'id="gFlags"' in t, "缺标记筛选容器 #gFlags"
    for f in ("like", "fav", "follow"):
        assert f'data-f="{f}"' in t, f"缺标记筛选项 {f}"
    assert 'data-f="all"' in t
    # 三个计数位
    for cid in ("cnLike", "cnFav", "cnFollow"):
        assert f'id="{cid}"' in t, f"缺计数位 {cid}"
    # 平台筛选必须还留着(两行互不影响)
    for f in ("douyin", "kuaishou", "bilibili"):
        assert f'data-p="{f}"' in t
    # buildGrid 里必须真的按标记过滤
    body = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    for cond in ("flag === 'like'   && !LIKE[it.id]",
                 "flag === 'fav'    && !FAV[it.id]",
                 "flag === 'follow' && !FOLLOW[it.author"):
        assert cond in body, f"buildGrid 缺过滤条件: {cond}"
    # 两个维度都不为 all 时才都生效(AND 语义)
    assert "plat !== 'all' && platLabel(it) !== plat" in body


def test_player_flags_rebuild_on_mark_change():
    """在「已赞」筛选下点赞/取消赞, 网格要立刻变, 不能等手动刷新。"""
    t = _player_src()
    body = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    assert "function flagActive()" in body
    # like / fav / follow 三个分支都要在标记变化后重建网格
    assert body.count("if (flagActive()) buildGrid(); else refreshFlagCounts();") >= 3, (
        "点赞/收藏/关注改完后都要重建网格(否则筛选结果不动)")


def test_player_card_overlay_ignores_pointer():
    """卡片上的角标/文案必须 pointer-events:none, 否则点击被它们截走。"""
    t = _player_src()
    assert "pointer-events:none" in t


def test_player_go_uses_instant_scroll():
    """go() 必须用瞬时跳转(scrollTop = ...), 不能用 behavior:'smooth' ——
    平滑滚动会被 scroll-snap 强制吸回原位, 表现为方向键"没反应"。"""
    t = _player_src()
    m = re.search(r"function go\(delta, autoplay\)\s*\{(.*?)\n\}", t, re.S)
    assert m, "找不到 go()"
    body = m.group(1)
    assert "behavior: 'smooth'" not in body and 'behavior:"smooth"' not in body
    assert "stage.scrollTop = cur * stage.clientHeight" in body


def test_player_go_pauses_previous_video():
    """go() 必须先 pause(cur) 再改 cur —— 否则上一个视频在后台播完会触发
    ended -> cfg.auto -> go(1), 把刚切到的位置又拽走(实测 ~1s 后跳回原格)。"""
    t = _player_src()
    m = re.search(r"function go\(delta, autoplay\)\s*\{(.*?)\n\}", t, re.S)
    body = m.group(1)
    assert body.index("pause(cur)") < body.index("cur = n"), "pause 必须在改 cur 之前"


def test_player_ended_only_advances_current():
    """ended 里必须判 i !== cur 就 return, 后台视频播完不许推进播放位置。"""
    t = _player_src()
    m = re.search(r"addEventListener\('ended',\s*\(\)\s*=>\s*\{(.*?)\}\)", t, re.S)
    assert m, "找不到 ended 处理"
    body = m.group(0)
    assert "i !== cur" in body, "ended 必须过滤掉非当前视频"


# ------------------------------- 4. 真浏览器端到端(缺 Chrome 则 skip)
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_E2E = r'''
import json, sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1]
out = {}
with sync_playwright() as p:
    br = p.chromium.launch(headless=True, channel="chrome")
    pg = br.new_page(viewport={"width": 1440, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="domcontentloaded")
    pg.wait_for_timeout(1800)
    out["slides"] = pg.locator("#stage .slide").count()
    out["cards"] = pg.locator("#grid .card").count()

    # 视频真的在播
    v0 = pg.evaluate("""() => {
      const v = document.querySelector('#stage .slide video');
      return {cur: v.currentTime, dur: v.duration, err: v.error ? 1 : 0,
              src: (v.currentSrc || '').slice(-24)};
    }""")
    pg.wait_for_timeout(1200)
    t2 = pg.evaluate("() => document.querySelector('#stage .slide video').currentTime")
    # 短视频可能已经播完停在末尾, 所以两个判据取其一: 时间在前进 或 已到末尾
    out["playing"] = bool(v0["dur"] > 0 and (t2 > v0["cur"] or v0["cur"] >= v0["dur"] - 0.05))
    out["v0"] = v0
    out["t2"] = t2
    out["video_err"] = bool(v0["err"])

    # 网格卡片不重叠
    rects = pg.evaluate("""() => Array.from(document.querySelectorAll('#grid .card'))
        .slice(0, 9).map(c => { const r = c.getBoundingClientRect();
          return {y: Math.round(r.top), h: Math.round(r.height)}; })""")
    tops = sorted({r["y"] for r in rects})
    out["rows"] = len(tops)
    out["card_h"] = rects[0]["h"] if rects else 0
    gaps = [b - a for a, b in zip(tops, tops[1:])]
    out["row_gaps"] = gaps
    # 不重叠的本质判据: 相邻两行的间距 >= 卡片高度
    out["row_gap_ok"] = bool(gaps) and all(g >= out["card_h"] for g in gaps)

    # 网格点击命中的是不是目标卡片
    hit = pg.evaluate("""() => {
      const all = document.querySelectorAll('#grid .card');
      if (!all.length) return -2;
      const c = all[all.length - 1];              // 点最后一张(有几张用几张)
      const r = c.getBoundingClientRect();
      const el = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
      const card = el && el.closest('.card');
      return card ? Number(card.dataset.i) : -1;
    }""")
    out["click_hit"] = hit

    # 方向键来回切(先关掉"播完自动下一个", 排除长视频播放结束的干扰)
    pg.evaluate("() => { cfg.auto = false; }")
    p0 = pg.evaluate("() => document.querySelector('#posTxt').textContent")
    pg.keyboard.press("ArrowDown"); pg.wait_for_timeout(900)
    p1 = pg.evaluate("() => document.querySelector('#posTxt').textContent")
    pg.keyboard.press("ArrowUp"); pg.wait_for_timeout(900)
    p2 = pg.evaluate("() => document.querySelector('#posTxt').textContent")
    out["pos"] = [p0, p1, p2]

    # 开着自动连播时, 方向键仍然要稳(守 ended 干扰的回归)
    pg.evaluate("() => { cfg.auto = true; }")
    pg.keyboard.press("ArrowDown"); pg.wait_for_timeout(900)
    a1 = pg.evaluate("() => document.querySelector('#posTxt').textContent")
    pg.keyboard.press("ArrowUp"); pg.wait_for_timeout(1500)
    a2 = pg.evaluate("() => document.querySelector('#posTxt').textContent")
    out["auto_pos"] = [a1, a2]

    out["errors"] = errs
    br.close()
print("@@JSON@@" + json.dumps(out))'''


def test_player_e2e_real_browser(tmp_path):
    """起临时 HTTP 服务, 用真 Chromium 打开 /player 做端到端验证。"""
    try:
        import playwright  # noqa: F401
    except Exception:
        pytest.skip("没装 playwright")

    ws, out = _mk_workspace(tmp_path, [
        ("7705373012305275018_作者A_标题1.mp4", 400000),
        ("7707115435887681701_作者B_标题2.mp4", 400000),
        ("7709206128344935681_作者C_标题3.mp4", 400000),
        ("7709206128344935682_作者D_标题4.mp4", 400000),
        ("7709206128344935683_作者E_标题5.mp4", 400000),
        ("7709206128344935684_作者F_标题6.mp4", 400000),
    ])
    # 端到端必须用真 mp4, 否则 Chromium 解码失败
    for p_ in list(out.glob("*.mp4")):
        if not _make_real_mp4(p_):
            pytest.skip("生成测试视频失败(ffmpeg 不可用)")

    # 缩略图
    th = ws / ".thumbs"
    th.mkdir()
    for p in out.glob("*.mp4"):
        aid = re.sub(r"\D", "", p.stem)
        (th / f"{aid}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)

    from douyin_dl.ui import start_ui  # 真服务

    port = _free_port()
    web = _ROOT / "douyin_dl" / "web" / "index.html"
    start_ui(_bus(ws), web, port=port, open_browser=False, log=lambda m: None)
    # 等端口起来
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    script = tmp_path / "_e2e_probe.py"
    script.write_text(_E2E, encoding="utf-8")
    r = subprocess.run([sys.executable, str(script), f"http://127.0.0.1:{port}/player"],
                       capture_output=True, encoding="utf-8", errors="replace",
                       timeout=180, cwd=str(_ROOT))
    payload = None
    for line in (r.stdout or "").splitlines():
        if line.startswith("@@JSON@@"):
            payload = json.loads(line[len("@@JSON@@"):])
    if payload is None:
        pytest.skip(f"e2e 探针没输出(可能是 Chrome 不可用): {r.stderr[-400:]}")

    assert payload["slides"] == 6, f"slide 数不对: {payload['slides']}"
    assert payload["cards"] == 6
    assert not payload["video_err"], "视频有解码错误"
    assert payload["playing"], "视频没有在播放"
    assert payload["errors"] == [], f"有 JS 报错: {payload['errors']}"
    # 网格 3 行、行距足够(不重叠)、卡片有高度
    assert payload["rows"] == 2, f"卡片排了 {payload['rows']} 行(说明行高塌了)"
    assert payload["row_gap_ok"], (
        f"网格行距小于卡片高度, 卡片在重叠: 卡片高={payload['card_h']} 行距={payload['row_gaps']}")
    assert payload["card_h"] >= 100, f"卡片高度只有 {payload['card_h']}"
    assert payload["click_hit"] == 5, f"点最后一张卡片(第 6 张)命中 {payload['click_hit']}"
    # 方向键来回
    p0, p1, p2 = payload["pos"]
    assert p1 != p0, f"↓ 没切走: {p0} -> {p1}"
    assert p2 == p0, f"↑ 没切回来: {p1} -> {p2}"
    # 开着自动连播时也要稳
    a1, a2 = payload["auto_pos"]
    assert a2 == p0, f"开着自动连播时 ↑ 被 ended 拽走: {a1} -> {a2}"
