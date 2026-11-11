# -*- coding: utf-8 -*-
"""
本地 Web 面板: 实时看下载进度、历史、设置。

为什么不引框架: 这套东西只跑在本机 127.0.0.1, 用标准库的
ThreadingHTTPServer + SSE 就够了, 不给用户装额外依赖。
"""

from __future__ import annotations

import json
import os
import struct
import zlib
import queue
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

# --------------------------------------------------------------- 播放器置前
# 我们是以 pythonw 后台进程的身份调 os.startfile 打开视频的, Windows 只会让
# "本来就在前台"的进程把前台权限传给子进程。所以播放器虽然起来了, 窗口却留在
# 后面, 用户还得自己去任务栏点一下。下面这几个函数就是补这一步。

# 常见播放器的进程名(小写)。命中就可以直接认出来是播放器窗口。
_MEDIA_PROC_NAMES = {
    "microsoft.media.player.exe",   # Win11 自带"媒体播放器"
    "wmplayer.exe",                  # 旧版 Windows Media Player
    "vlc.exe",
    "potplayermini64.exe",
    "potplayermini.exe",
    "potplayer64.exe",
    "mpc-hc64.exe",
    "mpc-be64.exe",
    "mpv.exe",
    "kmplayer.exe",
    "kmplayer64.exe",
    "qqplayer.exe",
    "stormplayer.exe",
    "nplayer.exe",
}


def _win_list() -> list[dict]:
    """列出所有"可见且有标题"的顶层窗口 -> [{hwnd, pid, title}]。

    纯 user32 查询, pythonw 没有自己的窗口也照样能调。
    """
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError:
        return []

    found: list[dict] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append({"hwnd": int(hwnd), "pid": int(pid.value),
                          "title": buf.value})
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(proto(_cb), 0)
    except Exception:
        return found
    return found


def _proc_name(pid: int) -> str:
    """pid -> 可执行文件名(小写); 拿不到返回空串。"""
    if os.name != "nt" or pid <= 0:
        return ""
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except Exception:
        return ""
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    except Exception:
        return ""
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def _force_foreground(hwnd: int) -> bool:
    """把指定窗口拉到最前面; 已经是前台就返回 True。

    后台进程直接 SetForegroundWindow 会被系统的"前台锁"拒绝, 所以:
      1) 先把当前线程输入挂到前台窗口所在线程上 (AttachThreadInput);
      2) 再补一次 Alt 键事件 —— 系统会认为用户有输入, 临时放开限制。
    这两招缺一个在某些场景下就不生效, 所以一起用。
    """
    if os.name != "nt" or not hwnd:
        return False
    import ctypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return False

    SW_RESTORE, SW_SHOW = 9, 5
    try:
        user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)
    except Exception:
        pass

    cur_tid = int(kernel32.GetCurrentThreadId())
    try:
        fg = int(user32.GetForegroundWindow() or 0)
    except Exception:
        fg = 0
    if fg == hwnd:
        return True

    attached: list[int] = []
    for tid in {int(user32.GetWindowThreadProcessId(fg, None) or 0) if fg else 0,
                int(user32.GetWindowThreadProcessId(hwnd, None) or 0)}:
        if tid and tid != cur_tid:
            try:
                if user32.AttachThreadInput(cur_tid, tid, True):
                    attached.append(tid)
            except Exception:
                pass

    VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
    try:
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        for tid in attached:
            try:
                user32.AttachThreadInput(cur_tid, tid, False)
            except Exception:
                pass

    try:
        return int(user32.GetForegroundWindow() or 0) == hwnd
    except Exception:
        return False


def _hosted_media(hwnd: int, cache: dict[int, str]) -> bool:
    """ApplicationFrameHost 的窗口里装的是不是播放器。

    Win11 的「媒体播放器」「电影和电视」都是 AppX 应用: 真正显示给你看的那层
    窗口属于 applicationframehost.exe, 播放器进程躲在里面当子窗口。所以只能
    顺着子窗口去查进程名, 否则会认不出来。
    """
    if os.name != "nt" or not hwnd:
        return False
    import ctypes
    from ctypes import wintypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError:
        return False

    hit = {"yes": False}
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(child, _lparam):
        pid = wintypes.DWORD(0)
        try:
            user32.GetWindowThreadProcessId(child, ctypes.byref(pid))
        except Exception:
            return True
        p = int(pid.value)
        if p not in cache:
            cache[p] = _proc_name(p)
        if cache[p] in _MEDIA_PROC_NAMES:
            hit["yes"] = True
            return False
        return True

    try:
        user32.EnumChildWindows(hwnd, proto(_cb), 0)
    except Exception:
        return False
    return hit["yes"]


def _activate_player(before: set[int], stem: str, timeout: float = 8.0) -> bool:
    """等播放器窗口出现, 然后把它拉到前台。

    before: 点"打开"之前的窗口句柄集合, 用来区分"新弹出来的"和"原本就在的"。
            播放器已经在跑时不会再开新窗口, 这时就直接把那个旧窗口提上来。
    stem:   视频文件名(不含扩展名), 标题里带它的窗口优先 —— 一个播放器开多个
            窗口时能挑对。
    """
    deadline = time.time() + timeout
    hint = (stem or "").strip().lower()[:12]
    name_cache: dict[int, str] = {}

    while time.time() < deadline:
        wins = _win_list()
        if wins:
            named = []
            for w in wins:
                pid = w["pid"]
                if pid not in name_cache:
                    name_cache[pid] = _proc_name(pid)
                pname = name_cache[pid]
                if pname in _MEDIA_PROC_NAMES:
                    named.append(w)
                elif pname == "applicationframehost.exe" and _hosted_media(w["hwnd"], name_cache):
                    named.append(w)

            fresh = [w for w in named if w["hwnd"] not in before]
            pool = fresh or named
            if not pool:
                # 认不出来的播放器(小众播放器/被关联到别的程序): 退一步,
                # 本次操作之后新冒出来的窗口大概率就是它。
                pool = [w for w in wins if w["hwnd"] not in before]

            if pool:
                pool.sort(key=lambda w: 0 if (hint and hint in w["title"].lower()) else 1)
                for w in pool[:3]:
                    if _force_foreground(w["hwnd"]):
                        return True
        time.sleep(0.25)
    return False


def _make_blank_png(size: int = 1, rgb: tuple[int, int, int] = (26, 32, 44)) -> bytes:
    """纯标准库造一张 1x1 深灰 PNG: 老记录没有封面时顶上, 免得控制台刷 404。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b"".join(bytes([0]) + bytes(rgb) * size for _ in range(size))
    sig = bytes([137]) + b"PNG" + bytes([13, 10, 26, 10])
    return (sig + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


_BLANK_IMG = _make_blank_png()

QUALITY_CHOICES = {
    "max": "最高画质",
    "1080": "1080P 及以下",
    "720": "720P 及以下",
    "480": "480P 及以下",
}


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------

def _safe_id(v) -> str:
    """抖音的 aweme_id 只会是纯数字。thumb/delete/open 都会把它拼进文件路径,
    这里统一拦一下, 免得被 ../ 之类的东西穿越出去。"""
    s = str(v or "").strip()
    return s if s.isdigit() and 5 <= len(s) <= 32 else ""


def load_settings(path: Path) -> dict:
    data = {"quality": "max", "open_browser": True, "auto_reopen": True, "by_author": False, "auto_rec": False}
    try:
        if path.exists():
            data.update(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        pass
    if data.get("quality") not in QUALITY_CHOICES:
        data["quality"] = "max"
    return data


def save_settings(path: Path, data: dict):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def filter_candidates(cands: list, pref: str) -> list:
    """按界面里选的画质偏好过滤候选, 过滤后为空就退回全部(总比下不下来强)。"""
    if not cands or pref in (None, "", "max"):
        return list(cands)
    try:
        limit = int(pref)
    except Exception:
        return list(cands)
    picked = [c for c in cands if not c.height or c.height <= limit]
    return picked or list(cands)


# ---------------------------------------------------------------------------
# 事件总线: 下载器往里写, 页面从这儿读
# ---------------------------------------------------------------------------

class EventBus:
    def __init__(self, workspace: Path, out_dir: Path, state_file: Path, log_file: Path):
        self.workspace = workspace
        self.out_dir = out_dir
        self.state_file = state_file
        self.log_file = log_file
        self.thumbs_dir = workspace / ".thumbs"
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)

        self.lock = threading.RLock()
        self.clients: list[queue.Queue] = []
        self.active: dict[str, dict] = {}
        self.waiting: list[dict] = []
        self.counters = {"ok": 0, "skip": 0, "fail": 0}
        self.failed: dict[str, dict] = {}          # aid -> {title, author, cover, why, time}
        self.on_retry = None                        # callable(aid) | None, 由主程序注入
        self.on_delete = None                       # callable(aid) -> bool, 由主程序注入(带锁删记录)
        self.on_auto_rec = None                     # callable(bool), 由主程序注入(实时切换 watcher.auto_recommend)
        self.status = {"running": False, "browser": "", "note": ""}
        self.started = time.time()
        self.max_speed = 0.0
        self._recent_cache: list[dict] | None = None
        self._recent_stamp = 0.0
        self._thumbs: dict[str, str] = {}

    # -- 订阅 -------------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self):
        snap = self.snapshot()
        with self.lock:
            dead = []
            for q in self.clients:
                try:
                    q.put_nowait(snap)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.clients.remove(q)

    # -- 写入 -------------------------------------------------------------
    def set_status(self, **kw):
        with self.lock:
            self.status.update(kw)
        self.publish()

    def add_waiting(self, aid: str, title: str = "", author: str = "", source: str = ""):
        with self.lock:
            if any(x["id"] == aid for x in self.waiting):
                return
            self.waiting.append({"id": aid, "title": title, "author": author,
                                 "source": source, "at": int(time.time())})
        self.publish()

    def pop_waiting(self, aid: str | None = None):
        with self.lock:
            if aid is None:
                self.waiting = []
            else:
                self.waiting = [x for x in self.waiting if x["id"] != aid]
        self.publish()

    def begin(self, aid: str, info: dict):
        with self.lock:
            self.active[aid] = {
                "id": aid,
                "title": info.get("title", ""),
                "author": info.get("author", ""),
                "cover": info.get("cover", ""),
                "quality": info.get("quality", ""),
                "total": info.get("size", 0),
                "done": 0,
                "pct": 0.0,
                "speed": 0.0,
                "phase": "downloading",
                "started": time.time(),
                "_last_t": time.time(),
                "_last_b": 0,
            }
            self.waiting = [x for x in self.waiting if x["id"] != aid]
        self.publish()

    def progress(self, aid: str, done: int, total: int):
        with self.lock:
            rec = self.active.get(aid)
            if not rec:
                return
            now = time.time()
            rec["done"] = done
            if total:
                rec["total"] = total
                rec["pct"] = round(min(100.0, done * 100.0 / total), 1)
            dt = now - rec["_last_t"]
            if dt >= 0.4:
                sp = (done - rec["_last_b"]) / dt
                rec["speed"] = round(max(0.0, sp), 0)
                rec["_last_t"] = now
                rec["_last_b"] = done
        self.publish()

    def finish(self, aid: str, ok: bool, info: dict | None = None):
        with self.lock:
            rec = self.active.pop(aid, None)
            if ok:
                self.counters["ok"] += 1
                # 之前失败过、后来重新收藏/重试成功了, 失败列表里那条得摘掉,
                # 不然面板永远挂着一个"失败", 用户以为没下好
                self.failed.pop(str(aid), None)
            else:
                self.counters["fail"] += 1
            self._recent_cache = None
        self.publish()
        return rec

    def skip(self):
        with self.lock:
            self.counters["skip"] += 1
        self.publish()

    def fail(self, aid: str, why: str, title: str = "", author: str = "", cover: str = ""):
        """下载失败: 记进失败列表, 面板上可以一键重试。"""
        with self.lock:
            self.failed[str(aid)] = {
                "id": str(aid), "why": why, "title": title, "author": author,
                "cover": cover, "time": int(time.time()),
            }
            self.failed = dict(list(self.failed.items())[-50:])   # 最多留 50 条
        self.publish()

    def retry(self, aid: str) -> bool:
        """把失败的那条重新交给下载器; 返回是否成功入队。"""
        fn = self.on_retry
        if not fn:
            return False
        try:
            ok = fn(str(aid))
        except Exception:
            ok = False
        if ok:
            with self.lock:
                self.failed.pop(str(aid), None)
            self.publish()
        return ok

    def set_speed(self, mbps: float):
        with self.lock:
            if mbps > self.max_speed:
                self.max_speed = round(mbps, 1)

    # -- 读取 -------------------------------------------------------------
    def _recent(self) -> list[dict]:
        """历史记录 = .state.json + downloads/_meta/*.json, 5 秒内不重复解析。"""
        now = time.time()
        if self._recent_cache is not None and now - self._recent_stamp < 5:
            return self._recent_cache
        out: list[dict] = []
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
        except Exception:
            state = {}
        meta_dir = self.out_dir / "_meta"
        meta_dir_default = self.workspace / "downloads" / "_meta"   # 改过下载目录后老元数据还在默认位置
        for aid, row in state.items():
            item = {
                "id": aid,
                "file": row.get("file", ""),
                "quality": row.get("quality", ""),
                "time": row.get("time", 0),
                "title": "",
                "author": "",
                "cover": "",
                "size": 0,
                "type": "video",
            }
            f = self.workspace / item["file"] if item["file"] else None
            if f and f.exists():
                try:
                    item["size"] = f.stat().st_size if f.is_file() else 0
                except Exception:
                    pass
                if f.is_dir():
                    item["type"] = "images"
            mp = meta_dir / f"{aid}.json"
            if not mp.exists():
                mp = meta_dir_default / f"{aid}.json"
            if mp.exists():
                try:
                    m = json.loads(mp.read_text(encoding="utf-8"))
                    item["title"] = m.get("desc", "")
                    item["author"] = m.get("author", "")
                    item["cover"] = m.get("cover", "")
                    item["duration"] = m.get("duration_ms", 0)
                    item["music"] = m.get("music", "")
                except Exception:
                    pass
            out.append(item)
        out.sort(key=lambda x: x.get("time") or 0, reverse=True)
        self._recent_cache = out
        self._recent_stamp = now
        return out

    def snapshot(self) -> dict:
        with self.lock:
            active = []
            for rec in self.active.values():
                r = {k: v for k, v in rec.items() if not k.startswith("_")}
                active.append(r)
            waiting = list(self.waiting)
            counters = dict(self.counters)
            status = dict(self.status)
            failed = list(self.failed.values())
        recent = self._recent()
        # 设置文件一次读够: 原来 quality/by_author/auto_reopen 各读一遍,
        # 每秒推一次快照就是每秒 3 次磁盘读取
        conf = load_settings(self.workspace / ".settings.json")
        today0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        return {
            "now": int(time.time()),
            "uptime": int(time.time() - self.started),
            "status": status,
            "counters": counters,
            "active": active,
            "waiting": waiting,
            "failed": failed,
            "recent": recent[:300],
            "total": len(recent),
            "today": sum(1 for x in recent if (x.get("time") or 0) >= today0),
            "bytes": sum(x.get("size") or 0 for x in recent),
            "max_speed": self.max_speed,
            "quality": conf.get("quality") or "max",
            "quality_choices": QUALITY_CHOICES,
            "out_dir": str(self.out_dir),
            "by_author": bool(conf.get("by_author")),
            "auto_reopen": bool(conf.get("auto_reopen", True)),
            "auto_rec": bool(conf.get("auto_rec", False)),
            "out_dir_configured": bool(conf.get("out_dir")),
        }

    @property
    def _quality(self) -> str:
        try:
            return load_settings(self.workspace / ".settings.json")["quality"]
        except Exception:
            return "max"

    # -- 缩略图 -----------------------------------------------------------
    def cover_url(self, aid: str) -> str:
        for x in self._recent():
            if x["id"] == aid:
                return x.get("cover") or ""
        return ""

    def thumb(self, aid: str) -> bytes | None:
        aid = _safe_id(aid)
        if not aid:
            return None
        p = self.thumbs_dir / f"{aid}.jpg"
        if p.exists() and p.stat().st_size > 0:
            try:
                return p.read_bytes()
            except Exception:
                return None
        url = self.cover_url(aid)
        if not url:
            return None
        try:
            r = requests.get(url, headers={"User-Agent": UA,
                                           "Referer": "https://www.douyin.com/"},
                             timeout=10)
            if r.status_code == 200 and r.content:
                try:
                    p.write_bytes(r.content)
                except Exception:
                    pass
                return r.content
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    bus: EventBus = None          # type: ignore
    web_file: Path = None         # type: ignore
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):    # 别把每个请求都打到控制台
        pass

    # -- 工具 -------------------------------------------------------------
    def _json(self, obj, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data: bytes, ctype: str, code: int = 200, cache: bool = True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 界面本体绝不能缓存, 否则改了前端还得手动清缓存
        self.send_header("Cache-Control",
                         "public, max-age=86400" if cache else "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)

    # -- GET --------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path in ("/", "/index.html"):
            try:
                html = self.web_file.read_bytes()
            except Exception as e:
                self._json({"error": f"界面文件读不到: {e}"}, 500)
                return
            self._bytes(html, "text/html; charset=utf-8", cache=False)
            return

        if path == "/api/state":
            self._json(self.bus.snapshot())
            return

        if path == "/api/events":
            self._sse()
            return

        if path == "/api/thumb":
            aid = _safe_id((qs.get("id") or [""])[0])
            data = self.bus.thumb(aid) if aid else None
            # 没封面时回 404(带 no-store), 让前端 thumbRetry 走 onerror 自动重试;
            # 之前回 200 的 1x1 占位图, 浏览器当成加载成功, 就永远不重试了 -> 新下载的没预览图
            if not data:
                self.send_response(404)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if data[:3] == b"\xff\xd8\xff":       # JPEG 魔数
                ctype = "image/jpeg"
            elif data[:4] == b"\x89PNG":
                ctype = "image/png"
            elif data[:6] in (b"GIF87a", b"GIF89a"):
                ctype = "image/gif"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                ctype = "image/webp"
            else:
                ctype = "application/octet-stream"
            self._bytes(data, ctype, cache=True)
            return

        if path == "/api/log":
            try:
                n = int((qs.get("lines") or ["120"])[0])
            except ValueError:
                n = 120
            n = max(1, min(n, 2000))
            try:
                lines = self.bus.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                lines = []
            self._json({"lines": lines[-n:]})
            return

        self._json({"error": "not found"}, 404)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.bus.subscribe()
        try:
            self._sse_write(self.bus.snapshot())
            while True:
                try:
                    snap = q.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse_write(snap)
        except Exception:
            pass
        finally:
            self.bus.unsubscribe(q)

    def _sse_write(self, obj):
        data = json.dumps(obj, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- POST -------------------------------------------------------------
    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}

        if u.path == "/api/open":
            self._json({"ok": self._open(body)})
            return

        if u.path == "/api/settings":
            path = self.bus.workspace / ".settings.json"
            cur = load_settings(path)
            if body.get("quality") in QUALITY_CHOICES:
                cur["quality"] = body["quality"]
            if isinstance(body.get("by_author"), bool):
                cur["by_author"] = body["by_author"]
            if isinstance(body.get("auto_reopen"), bool):
                cur["auto_reopen"] = body["auto_reopen"]
            # v1.4: 自动下推荐页开关。保存时立即同步给 watcher, 不重启就生效。
            if isinstance(body.get("auto_rec"), bool):
                cur["auto_rec"] = body["auto_rec"]
                cb = getattr(self.bus, "on_auto_rec", None)
                if callable(cb):
                    try:
                        cb(body["auto_rec"])
                    except Exception:
                        pass
            # 下载目录可调: 必须是绝对路径; 相对路径按工作区补全后存。
            # 建不出来(盘不存在/没权限)就拒绝, 不留下一个下载会悄悄失败的设置。
            if "out_dir" in body:
                raw = str(body.get("out_dir") or "").strip()
                if raw:
                    p = Path(raw).expanduser()
                    if not p.is_absolute():
                        p = (self.bus.workspace / p).resolve()
                    try:
                        p.mkdir(parents=True, exist_ok=True)
                        test = p / ".dy_write_test"
                        test.write_text("x", encoding="utf-8")
                        test.unlink()
                    except Exception:
                        self._json({"ok": False, "error": "目录建不出来或不可写, 没有保存"})
                        return
                    cur["out_dir"] = str(p)
                    self.bus.out_dir = p          # 面板的「打开文件夹」等立即跟随
                    try:
                        (p / "_meta").mkdir(exist_ok=True)
                    except Exception:
                        pass
                else:
                    # 清空 = 恢复默认 downloads/
                    cur.pop("out_dir", None)
                    self.bus.out_dir = self.bus.workspace / "downloads"
            save_settings(path, cur)
            self.bus.publish()
            self._json({"ok": True, "settings": cur})
            return

        if u.path == "/api/retry":
            ok = self.bus.retry(_safe_id(body.get("id")))
            self._json({"ok": ok})
            return

        if u.path == "/api/delete":
            self._json({"ok": self._delete(body)})
            return

        self._json({"error": "not found"}, 404)

    def _delete(self, body: dict) -> bool:
        """从面板删掉一条记录: 删 mp4/图文文件 + _meta + .state.json 条目 + 缩略图。"""
        aid = _safe_id(body.get("id"))
        if not aid:
            return False
        removed = False
        try:
            for x in self.bus._recent():
                if x.get("id") != aid:
                    continue
                f = x.get("file")
                if f:
                    t = self.bus.workspace / f
                    if t.is_file():
                        t.unlink(); removed = True
                    elif t.is_dir():
                        import shutil
                        shutil.rmtree(t, ignore_errors=True)
                        removed = True
                    # _meta 跟文件放在一起(开了按作者分文件夹就是 downloads/作者/_meta/)
                    d = t.parent / "_meta" / f"{aid}.json"
                    if d.is_file():
                        d.unlink()
                removed = True
            # 记录一定要交给主程序删: 下载线程持有 State 的内存副本并整个重写文件,
            # 面板这边直接改 .state.json 会被它下一次 mark() 覆盖回去(删掉的记录复活)
            if callable(self.bus.on_delete):
                try:
                    self.bus.on_delete(aid)
                except Exception:
                    pass
            else:
                st = self.bus.state_file
                if st.exists():
                    data = json.loads(st.read_text(encoding="utf-8"))
                    if aid in data:
                        data.pop(aid, None)
                        st.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            th = self.bus.thumbs_dir / f"{aid}.jpg"
            if th.exists():
                th.unlink()
            self.bus._recent_cache = None
            self.bus.publish()
        except Exception:
            return removed
        return removed

    def _open(self, body: dict) -> bool:
        what = body.get("what")
        try:
            if what == "folder":
                os.startfile(str(self.bus.out_dir))          # noqa: S606
                return True
            if what == "file":
                aid = _safe_id(body.get("id"))
                for x in self.bus._recent():
                    if x["id"] == aid and x.get("file"):
                        target = self.bus.workspace / x["file"]
                        if target.is_dir():
                            os.startfile(str(target))         # noqa: S606 图文作品是目录, 只能开目录
                        else:
                            before = {w["hwnd"] for w in _win_list()}
                            os.startfile(str(target))         # noqa: S606 用系统默认播放器直接播放视频
                            # 播放器起来后把它拉到前台, 免得窗口躲在后面还要用户自己点。
                            # 放线程里做, 不拖慢面板这次的请求。
                            threading.Thread(
                                target=_activate_player,
                                args=(before, target.stem),
                                name="dy-player-front",
                                daemon=True,
                            ).start()
                        return True
            if what == "reveal":
                aid = _safe_id(body.get("id"))
                for x in self.bus._recent():
                    if x["id"] == aid and x.get("file"):
                        target = self.bus.workspace / x["file"]
                        if target.is_file():
                            subprocess.Popen(["explorer", "/select,", str(target)])
                        else:
                            os.startfile(str(target))         # noqa: S606
                        return True
            if what == "log":
                os.startfile(str(self.bus.log_file))          # noqa: S606
                return True
        except Exception:
            return False
        return False


def start_ui(bus: EventBus, web_file: Path, port: int = 8899,
             open_browser: bool = True, log=print) -> str:
    """起面板服务, 返回访问地址。

    严格单实例: 8899 已被占用说明面板已经在跑(守护重启/双击 bat 都会遇到),
    这时不再起新面板也不弹浏览器, 直接返回空 —— 用户浏览器里那个标签页还活着,
    SSE 会自动重连到新进程。"""
    _Handler.bus = bus
    _Handler.web_file = web_file
    # 先探测是不是已有面板在跑
    try:
        import urllib.request as _ur
        with _ur.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            if r.status == 200:
                log(f"  面板已在运行: http://127.0.0.1:{port}/ (不重复打开)")
                return ""
    except Exception:
        pass
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        # 8899 被别的程序占了(不是我们的面板): 不顺延端口, 避免开出多个面板
        log(f"  ! 面板端口 {port} 被其他程序占用, 本次不启动面板: {e}")
        return ""

    threading.Thread(target=server.serve_forever, daemon=True, name="dy-ui").start()
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        def _open():
            time.sleep(0.8)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()
    return url
