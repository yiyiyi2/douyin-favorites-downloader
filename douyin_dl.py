#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音收藏自动下载 —— 主程序

用法:
    python douyin_dl.py                 # 启动, 然后去抖音点收藏
    python douyin_dl.py --download-all  # 启动时先把现有收藏补下回来
    python douyin_dl.py --url <链接>     # 只下这一个视频, 不启动监听
    python douyin_dl.py --check         # 检查浏览器登录状态
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
STATE_FILE = WORKSPACE / ".state.json"
PROFILE_DIR = WORKSPACE / ".chrome_profile"
LOG_FILE = WORKSPACE / "douyin_dl.log"
SETTINGS_FILE = WORKSPACE / ".settings.json"
WEB_FILE = WORKSPACE / "douyin_dl" / "web" / "index.html"

from douyin_dl.ui import (
    EventBus,
    filter_candidates,
    load_settings,
    save_settings,
    start_ui,
)

# 下载目录的运行时取值: 设置文件 out_dir > 启动参数 > 工作区/downloads。
# OUT_DIR 在 main() 里按上面顺序初始化一次; 下载线程里每次都经 get_out_dir() 取,
# 这样面板里改了目录, 不重启主进程也能对下一个视频生效。
OUT_DIR = WORKSPACE / "downloads"


def get_out_dir() -> Path:
    try:
        v = str(load_settings(SETTINGS_FILE).get("out_dir") or "").strip()
    except Exception:
        v = ""
    if not v:
        return OUT_DIR
    p = Path(v).expanduser()
    if not p.is_absolute():
        p = (WORKSPACE / p).resolve()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        return OUT_DIR          # 目录建不出来(盘不存在/没权限)就还用旧的
    return p


def save_settings_file(path, data):
    save_settings(path, data)


_log_lock = threading.Lock()

from douyin_dl.core import (
    build_filename,
    build_session,
    download,
    parse_aweme,
    probe_size,
    safe_name,
    verify_video,
    VideoInfo,
)


def refetch_aweme(aid: str, session) -> dict | None:
    """
    面板「重试」用: 从调试端口读已登录 Cookie, 用无头浏览器开一次视频详情页,
    让页面自己发签名请求, 拿最新的 aweme 数据。抓不到返回 None。
    """
    # 同一时刻只允许一个无头浏览器在抓详情: 下载是多线程的,
    # 并发各起一个 chromium 会互相拖慢(资源抖动/超时), 之前推荐页批量入队时
    # 一半抓不到就是这个原因。全局锁串行化, 每次抓取反而更快更稳。
    with _refetch_lock:
        return _refetch_aweme_locked(aid)


_refetch_lock = threading.Lock()


def _refetch_aweme_locked(aid: str) -> dict | None:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=3):
            pass
    except Exception:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as pw:
            br = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = br.contexts[0]
            cooked = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                       "path": c.get("path", "/")} for c in ctx.cookies()
                      if "douyin" in (c.get("domain") or "")]
            br.close()
        result: dict = {}

        def _grab():
            with sync_playwright() as pw:
                hbr = pw.chromium.launch(headless=True, channel="chrome",
                                         args=["--disable-blink-features=AutomationControlled",
                                               "--mute-audio", "--no-first-run"])
                hctx = hbr.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}, locale="zh-CN")
                hctx.add_cookies(cooked)
                pg = hctx.new_page()

                def on_resp(r):
                    try:
                        if "/aweme/detail/" in r.url and result == {}:
                            d = r.json()
                            det = (d.get("data") or {}).get("aweme_detail") or d.get("aweme_detail")
                            if isinstance(det, dict) and (det.get("video") or det.get("images")):
                                result.update(det)
                    except Exception:
                        pass
                pg.on("response", on_resp)
                pg.goto(f"https://www.douyin.com/video/{aid}",
                        wait_until="domcontentloaded", timeout=30000)
                for _ in range(24):
                    if result:
                        break
                    pg.wait_for_timeout(500)
                hbr.close()

        import threading as _th
        t = _th.Thread(target=_grab, daemon=True)
        t.start()
        t.join(timeout=45)
        return dict(result) or None
    except Exception:
        return None


def log(msg: str = ""):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass   # pythonw 下没有 stdout, print 会抛错; 守护模式全是 pythonw
        try:
            # 日志只增不减, 跑几个月能把磁盘吃掉。超过 5MB 就滚一份 .1(只留一代)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
                bak = LOG_FILE.with_suffix(".log.1")
                try:
                    if bak.exists():
                        bak.unlink()
                    LOG_FILE.rename(bak)
                except Exception:
                    pass
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 状态: 记录已下载的 aweme_id, 避免重复下载
# ---------------------------------------------------------------------------

class State:
    def __init__(self, path: Path):
        self.path = path
        self.done: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.load()

    def load(self):
        if self.path.exists():
            try:
                self.done = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.done = {}

    def has(self, aweme_id: str) -> bool:
        with self.lock:
            return aweme_id in self.done

    def remove(self, aweme_id: str) -> bool:
        """删一条记录并落盘。面板「删除」走这里, 和 mark() 共用一把锁, 不会互相覆盖。"""
        with self.lock:
            if str(aweme_id) not in self.done:
                return False
            self.done.pop(str(aweme_id), None)
            try:
                self.path.write_text(
                    json.dumps(self.done, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            except Exception:
                pass
            return True

    def mark(self, aweme_id: str, path: Path, quality: str):
        with self.lock:
            self.done[aweme_id] = {
                "file": str(path.relative_to(WORKSPACE)) if path.is_relative_to(WORKSPACE) else str(path),
                "quality": quality,
                "time": int(time.time()),
            }
            try:
                self.path.write_text(
                    json.dumps(self.done, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 下载器线程
# ---------------------------------------------------------------------------

class Downloader(threading.Thread):
    def __init__(self, state: State, session, log_fn=log, bus=None):
        super().__init__(daemon=True)
        self.q: queue.Queue[tuple[dict, str]] = queue.Queue()
        self.state = state
        self.session = session
        self.log = log_fn
        self.bus = bus
        self.count_ok = 0
        self.count_skip = 0
        self.count_fail = 0
        self._rec_prefetch: dict[str, int] = {}   # 推荐页详情预取的重试计数

    # -- 面板事件小工具 ------------------------------------------------
    def _ev(self, name, *a):
        if not self.bus:
            return
        try:
            getattr(self.bus, name)(*a)
        except Exception:
            pass

    def submit(self, aweme: dict, source: str):
        self.q.put((aweme, source))
        if self.bus:
            aid = str(aweme.get("aweme_id") or "")
            if aid:
                try:
                    _i = parse_aweme(aweme)
                    self._ev("add_waiting", aid, _i.desc[:60], _i.author, source)
                except Exception:
                    self._ev("add_waiting", aid, "", "", source)

    def run(self):
        while True:
            aweme, source = self.q.get()
            try:
                self.process(aweme, source)
            except Exception as e:  # noqa: BLE001
                self.log(f"  ! 处理出错: {e}")
                self.count_fail += 1
            finally:
                self.q.task_done()

    def delete_record(self, aid: str) -> bool:
        """面板删除一条记录: 只负责 .state.json, 文件由面板那边删。"""
        try:
            return self.state.remove(str(aid))
        except Exception:
            return False

    def retry(self, aid: str) -> bool:
        """面板点重试: 用无头浏览器重新抓一次详情, 抓到就重新入队。"""
        try:
            aw = refetch_aweme(aid, self.session)
        except Exception as e:
            self.log(f"  ! 重试 {aid} 抓取失败: {e}")
            return False
        if not aw:
            self.log(f"  ! 重试 {aid}: 拿不到视频信息")
            return False
        self.q.put((aw, "重试"))
        return True

    def process(self, aweme: dict, source: str):
        # 推荐页(feed/related)列表里的候选 URL 带 ply_type=4&tk=webid,
        # 是页面播放器的内部令牌流, requests 直接下会 403。
        # 必须先走无头浏览器抓一次视频详情页, 换成 detail 接口的可直下 URL。
        # 抓不到就重新排队等下一轮(最多 3 次), 绝不用列表自带地址硬试 ——
        # 那些地址带 ply_type=4&tk=webid, 直下必 403, 纯浪费带宽和重试次数。
        if source == "推荐页":
            aid0 = str(aweme.get("aweme_id") or "")
            tried = self._rec_prefetch.get(aid0, 0)
            detail = None
            try:
                detail = refetch_aweme(aid0, self.session)
            except Exception as e:  # noqa: BLE001
                self.log(f"  · 推荐页预取详情异常({e})")
            if detail:
                self._rec_prefetch.pop(aid0, None)
                aweme = detail
                self.log("  · 已换成详情页地址(可直下)")
            else:
                if tried < 2:
                    # 没抓到: 等 8 秒重新排队, 给下一个视频让出抓取通道
                    self._rec_prefetch[aid0] = tried + 1
                    self.log(f"  · 详情抓取没就绪, 8 秒后重新排队({tried + 1}/3)")
                    self._ev("pop_waiting", aid0)
                    _th2 = threading.Timer(
                        8.0, lambda: self.q.put((aweme, "推荐页")))
                    _th2.daemon = True
                    _th2.start()
                    return
                self._rec_prefetch.pop(aid0, None)
                self.log("  ! 重试 3 次都没抓到详情, 放弃这个推荐视频(不浪费 403 重试)")
                self._ev("pop_waiting", aid0)
                self._ev("fail", aid0, "详情页抓取失败(推荐页)")
                self.count_fail += 1
                return

        info = parse_aweme(aweme)
        if not info.aweme_id:
            return

        if self.state.has(info.aweme_id):
            self.count_skip += 1
            self._ev("pop_waiting", info.aweme_id)
            self._ev("skip")
            return

        label = f"{info.author} - {info.desc[:40]}" if info.author else info.desc[:50]
        self.log("")
        self.log(f"▼ [{source}] {label or info.aweme_id}")

        if info.is_image_post and not info.candidates:
            self._save_images(info)
            return

        # 界面里可以限制最高画质(省流量); 没设就是最高画质
        pref = load_settings(SETTINGS_FILE).get("quality", "max")
        cands = filter_candidates(info.candidates, pref)

        # 按画质从高到低排序。首选失败(常见的 403: 有的地址带 tk=webid,
        # 需要播放器内部令牌, 直接下载会被拒)就依次退到下一个候选。
        order = sorted(cands, key=lambda c: c.score(), reverse=True)
        best = order[0] if order else None
        if not best:
            self.log("  ! 没有可用的视频地址, 跳过")
            self.count_fail += 1
            self._ev("pop_waiting", info.aweme_id)
            self._ev("finish", info.aweme_id, False)
            self._ev("fail", info.aweme_id, "没有可用的视频地址",
                     info.desc[:50], info.author,
                     (info.cover_urls[0] if info.cover_urls else ""))
            return

        # 探一下体积, 顺便验证地址可用
        size = probe_size(self.session, best.url)
        res = f"{best.width}x{best.height}" if best.width else "?"
        cands = len(info.candidates)
        self.log(
            f"  画质: {res}  码率={best.bitrate or '?'}  "
            f"候选={cands} 个  {size / 1048576:.1f} MB" if size else
            f"  画质: {res}  候选={cands} 个  大小未知"
        )

        stem = build_filename(info)
        dest_dir = get_out_dir()
        # 面板里开了「按作者分文件夹」就归类: 下载目录/作者名/xxx.mp4
        if info.author and load_settings(SETTINGS_FILE).get("by_author"):
            safe = "".join(ch for ch in info.author if ch not in '\\/:*?"<>|').strip() or "未知作者"
            dest_dir = OUT_DIR / safe[:40]
            dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{stem}.mp4"

        # 已经有同名文件(可能是上次没记进 state)就直接登记
        if dest.exists() and dest.stat().st_size > 0:
            self.log(f"  已存在, 跳过: {dest.name}")
            self.state.mark(info.aweme_id, dest, f"{res}")
            self.count_skip += 1
            self._ev("pop_waiting", info.aweme_id)
            self._ev("skip")
            return

        self.log(f"  下载中 -> {dest_dir.name}/{dest.name}")
        self._ev("begin", info.aweme_id, {
            "title": info.desc[:70] or info.aweme_id,
            "author": info.author,
            "cover": (info.cover_urls[0] if info.cover_urls else ""),
            "quality": res,
            "size": size,
        })
        t0 = time.time()
        ok = False
        aid = info.aweme_id
        for i, cand in enumerate(order[:5]):
            if i:
                self.log(f"  换候选 {i + 1}/{min(len(order), 5)}: "
                         f"{cand.width}x{cand.height} 码率={cand.bitrate or '?'}")
            if download(self.session, cand.url, dest, log=self.log,
                        on_progress=lambda d, t: self._ev("progress", aid, d, t)):
                ok = True
                best = cand
                break
        if not ok:
            self.log("  × 所有候选地址都下载失败")
            self.count_fail += 1
            self._ev("finish", info.aweme_id, False)
            self._ev("fail", info.aweme_id, "所有候选地址都下载失败",
                     info.desc[:50], info.author,
                     (info.cover_urls[0] if info.cover_urls else ""))
            return

        if not verify_video(dest):
            # 不能留: 下次再下同一视频会命中下面的「已存在, 跳过」, 于是永远是个坏文件
            self.log("  × 文件校验失败(可能不是有效视频), 已删掉")
            try:
                dest.unlink()
            except Exception:
                pass
            self.count_fail += 1
            self._ev("finish", info.aweme_id, False)
            self._ev("fail", info.aweme_id, "文件校验失败",
                     info.desc[:50], info.author,
                     (info.cover_urls[0] if info.cover_urls else ""))
            return

        dt = time.time() - t0
        mb = dest.stat().st_size / 1048576
        speed = mb / dt if dt > 0 else 0
        self.log(f"  ✔ 完成  {mb:.1f} MB  {dt:.1f}s  ({speed:.1f} MB/s)")

        # 顺手存一份元数据, 方便以后查
        self._save_meta(info, dest, res)
        self.state.mark(info.aweme_id, dest, res)
        self.count_ok += 1
        self._ev("set_speed", speed)
        self._ev("finish", info.aweme_id, True, {"size": dest.stat().st_size})

    def _save_meta(self, info: VideoInfo, dest: Path, quality: str):
        meta = {
            "aweme_id": info.aweme_id,
            "desc": info.desc,
            "author": info.author,
            "author_id": info.author_id,
            "create_time": info.create_time,
            "duration_ms": info.duration,
            "music": f"{info.music_title} - {info.music_author}".strip(" -"),
            "file": dest.name,
            "quality": quality,
            "cover": (info.cover_urls[0] if info.cover_urls else ""),
            "all_candidates": [
                {"w": c.width, "h": c.height, "bitrate": c.bitrate, "gear": c.gear_name}
                for c in info.candidates
            ],
            "downloaded_at": int(time.time()),
        }
        meta_dir = get_out_dir() / "_meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        try:
            (meta_dir / f"{info.aweme_id}.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _save_images(self, info: VideoInfo):
        """图文作品: 逐张下原图。"""
        folder = get_out_dir() / build_filename(info)
        folder.mkdir(parents=True, exist_ok=True)
        ok = 0
        for i, u in enumerate(info.image_urls, 1):
            ext = ".jpg"
            if ".png" in u.lower():
                ext = ".png"
            elif ".webp" in u.lower():
                ext = ".webp"
            p = folder / f"{i:02d}{ext}"
            if download(self.session, u, p, log=self.log):
                ok += 1
        self.log(f"  ✔ 图文作品: 已保存 {ok}/{len(info.image_urls)} 张 -> {folder.name}/")
        self._ev("pop_waiting", info.aweme_id)   # 图集不走 begin, 得自己把排队项摘掉
        if ok:
            self.state.mark(info.aweme_id, folder, "images")
            self.count_ok += 1
            self._ev("finish", info.aweme_id, True)
        else:
            self.count_fail += 1
            self._ev("finish", info.aweme_id, False)


# ---------------------------------------------------------------------------
# 单链接下载(不依赖浏览器)
# ---------------------------------------------------------------------------

def download_single(url: str, state: State) -> int:
    """
    直接下载一个链接。
    先用 yt-dlp 抓详情(它内置了抖音解析), 失败再提示需要用 --watch 模式。
    """
    import subprocess

    log(f"解析链接: {url}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(OUT_DIR / "%(uploader)s_%(title).70B_%(id)s.%(ext)s"),
        "--print", "after_move:filepath",
        "--no-warnings",
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        err_lines = [x for x in (r.stderr or "").strip().splitlines() if x.strip()]
        log("  ! yt-dlp 解析失败:")
        log("    " + (err_lines[-1] if err_lines else f"退出码 {r.returncode}, 无错误输出"))
        return 1
    for line in (r.stdout or "").splitlines():
        if line.strip():
            log(f"  ✔ 已保存: {line.strip()}")
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _focus_existing_window(port: int = 9222) -> bool:
    """
    第二次双击启动时, 把已经在跑的那个抖音窗口提到最前面。

    不然用户只会看到"已经在运行了"然后什么都没有, 很像程序坏了。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=5000)
            pages = [pg for ctx in browser.contexts for pg in ctx.pages
                     if not pg.is_closed()]
            pages.sort(key=lambda pg: 0 if "douyin.com" in (pg.url or "") else 1)
            for pg in pages:
                try:
                    pg.bring_to_front()
                    return True
                except Exception:
                    continue
    except Exception:
        return False
    return False


def _acquire_instance_lock():
    """
    保证同一时间只有一个监听进程。

    踩过的坑: 重复启动会起来两个进程, 都挂在同一个浏览器上, 互相抢页面、
    同一条收藏被下载两遍, 而且关掉其中一个浏览器就整个不能用了。
    用 Windows 文件锁实现, 进程被强杀时锁也会自动释放。
    """
    import os

    try:
        import msvcrt
    except ImportError:
        return object()  # 非 Windows 直接放行

    try:
        fd = os.open(str(WORKSPACE / ".watcher.lock"), os.O_RDWR | os.O_CREAT)
    except OSError:
        return object()  # 拿不到锁文件也不该阻止正常使用

    def _close(f):
        try:
            os.close(f)
        except OSError:
            pass

    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        # 锁被占: 先确认是不是真的有活进程, 别让残留锁挡住正常启动。
        # (文件锁理论上随进程消失, 但进程被强杀时偶尔会留下死锁文件)
        if not _has_live_watcher():
            try:
                _close(fd)
                os.remove(str(WORKSPACE / ".watcher.lock"))
                fd = os.open(str(WORKSPACE / ".watcher.lock"), os.O_RDWR | os.O_CREAT)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return fd
            except OSError:
                pass
        _close(fd)
        return None
    return fd


def _has_live_watcher() -> bool:
    """有没有真的 douyin_dl.py 进程在跑(按命令行匹配, 不看锁文件)。

    注意 daemon 守护模式下下载进程跑在 pythonw.exe 里, 只查 python.exe 会漏,
    然后误判"没有活进程"把残留锁删掉, 造成两个守护各拉起一个监听互相抢。"""
    try:
        import subprocess
        out = subprocess.run(
            ["wmic", "process", "where",
             "name='python.exe' or name='pythonw.exe'", "get", "commandline"],
            capture_output=True, text=True, timeout=10,
        ).stdout.lower()
        return "douyin_dl.py" in out or "daemon.py" in out
    except Exception:
        return False  # 探测失败时保守认为没有, 允许覆盖残留锁


_LOCK = None


def main():
    global _LOCK

    ap = argparse.ArgumentParser(
        description="抖音收藏自动下载(最高画质 / 无水印)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", help="只下载指定链接, 不启动监听")
    ap.add_argument("--download-all", action="store_true",
                    help="启动时先把当前收藏里的所有视频补下回来")
    ap.add_argument("--check", action="store_true", help="只检查登录状态")
    ap.add_argument("--port", type=int, default=9222, help="Chrome 调试端口")
    ap.add_argument("--headless", action="store_true", help="无头模式(需要已登录过)")
    ap.add_argument("--output", help="输出目录 (默认 ./downloads)")
    ap.add_argument("--ui-port", type=int, default=8899, help="本地面板端口 (默认 8899)")
    ap.add_argument("--no-ui", action="store_true", help="不启动本地面板, 也不打开浏览器")
    ap.add_argument("--no-open", action="store_true", help="启动面板但不要自动打开浏览器")
    args = ap.parse_args()

    global OUT_DIR
    # 启动时: 命令行 --output 优先, 其次设置文件里的 out_dir, 最后工作区/downloads
    settings0 = load_settings(SETTINGS_FILE)
    if args.output:
        OUT_DIR = Path(args.output).resolve()
        settings0["out_dir"] = str(OUT_DIR)          # 命令行指定时写回设置, 面板能看到
        save_settings_file(SETTINGS_FILE, settings0)
    elif settings0.get("out_dir"):
        OUT_DIR = Path(str(settings0["out_dir"])).expanduser()
        if not OUT_DIR.is_absolute():
            OUT_DIR = (WORKSPACE / OUT_DIR).resolve()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    state = State(STATE_FILE)
    session = build_session()

    log("=" * 62)
    log("  抖音收藏自动下载  |  最高画质 · 无水印")
    log("=" * 62)
    log(f"  工作区: {WORKSPACE}")
    log(f"  输出到: {OUT_DIR}")
    log(f"  已记录: {len(state.done)} 个视频")
    log("")

    if args.url:
        return download_single(args.url, state)

    # 延迟导入, 避免 --url 模式也要加载 playwright
    from douyin_dl.watcher import BrowserWatcher

    # 锁必须放在开面板/弹浏览器之前: 不然双击两次会把面板也起两份
    # (旧顺序里第二次启动已经把面板/浏览器弹出来了才在这里退出)。
    _LOCK = _acquire_instance_lock()
    if _LOCK is None:
        log("  ! 已经有一个下载进程在运行了, 这次不重复启动")
        if _focus_existing_window():
            log("    已经把抖音窗口切到前面了, 直接刷就行")
        else:
            log("    没找到正在运行的抖音窗口: 请把原来那个黑色窗口关掉, 再双击启动一次")
        return 3

    bus = EventBus(WORKSPACE, OUT_DIR, STATE_FILE, LOG_FILE)
    ui_url = ""
    if not args.no_ui:
        settings = load_settings(SETTINGS_FILE)
        want_open = bool(settings.get("open_browser", True)) and not args.no_open
        ui_url = start_ui(bus, WEB_FILE, port=args.ui_port,
                          open_browser=want_open, log=log)
        if ui_url:
            bus.set_status(running=False, browser="", note="启动中")
            log(f"  面板: {ui_url}")
            log("")

    dl = Downloader(state, session, bus=bus)
    dl.start()
    bus.on_retry = dl.retry          # 面板「重试」按钮 -> 重新抓详情再入队
    bus.on_delete = dl.delete_record  # 面板「删除」按钮 -> 带锁从 .state.json 摘掉

    watcher = BrowserWatcher(
        profile_dir=PROFILE_DIR,
        port=args.port,
        on_aweme=lambda aw, src: dl.submit(aw, src),
        # 抖音视频地址带 tk=webid, 绑定登录态, 下载必须带同一套 Cookie, 否则 403
        on_cookie=lambda c: session.headers.update({"Cookie": c}),
        log=log,
        headless=args.headless,
        on_status=lambda kw: bus.set_status(**kw),
    )
    watcher.on_fail = lambda aid, why: bus.fail(aid, why)   # 抓不到详情也能在面板失败列表里看到

    if args.check:
        # 只检查登录: 起浏览器 -> 查 cookie -> 退出
        if not watcher.start_chrome():
            return 2
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
            ctx = b.contexts[0] if b.contexts else b.new_context()
            ok = BrowserWatcher.check_login(ctx)
        log(f"  登录状态: {'✔ 已登录' if ok else '✘ 未登录'}")
        return 0 if ok else 1

    if args.download_all:
        log("  模式: 先补下现有收藏, 再进入监听")
        watcher.download_all_on_start = True
    else:
        watcher.download_all_on_start = False

    # v1.4: 自动下推荐页(设置里开关, 默认关)。启动时读设置, 面板改设置时实时切换。
    _conf0 = load_settings(SETTINGS_FILE)
    watcher.auto_recommend = bool(_conf0.get("auto_rec", False))
    if watcher.auto_recommend:
        log("  模式: 自动下载推荐页视频已开启(设置里可随时关)")
    bus.on_auto_rec = lambda v: setattr(watcher, "auto_recommend", bool(v))

    rc = 0
    try:
        rc = watcher.run()
        if rc == 7:
            # 用户主动关闭(自动重开=关 且抖音全部关闭): 让 daemon 知道别再拉起
            log("  已按设置停止监听。下次双击 启动收藏下载.bat 再开始。")
    except KeyboardInterrupt:
        pass
    finally:
        log("")
        log(f"  本次统计: 成功 {dl.count_ok} · 跳过 {dl.count_skip} · 失败 {dl.count_fail}")
        log(f"  文件在: {OUT_DIR}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
