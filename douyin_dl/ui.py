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

def load_settings(path: Path) -> dict:
    data = {"quality": "max", "open_browser": True}
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
        recent = self._recent()
        today0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        return {
            "now": int(time.time()),
            "uptime": int(time.time() - self.started),
            "status": status,
            "counters": counters,
            "active": active,
            "waiting": waiting,
            "failed": list(self.failed.values()),
            "recent": recent[:300],
            "total": len(recent),
            "today": sum(1 for x in recent if (x.get("time") or 0) >= today0),
            "bytes": sum(x.get("size") or 0 for x in recent),
            "max_speed": self.max_speed,
            "quality": self._quality,
            "quality_choices": QUALITY_CHOICES,
            "out_dir": str(self.out_dir),
            "by_author": bool(load_settings(self.workspace / ".settings.json").get("by_author")),
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
            aid = (qs.get("id") or [""])[0]
            data = self.bus.thumb(aid) if aid else None
            # 没有封面时回一张 1x1 灰色图, 免得浏览器控制台刷一堆 404
            if not data:
                data, ctype = _BLANK_IMG, "image/png"
            elif data[:3] == b"\xff\xd8\xff":       # JPEG 魔数
                ctype = "image/jpeg"
            elif data[:4] == b"\x89PNG":
                ctype = "image/png"
            elif data[:6] in (b"GIF87a", b"GIF89a"):
                ctype = "image/gif"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                ctype = "image/webp"
            else:
                ctype = "application/octet-stream"
            self._bytes(data, ctype, cache=bool(data and data is not _BLANK_IMG))
            return

        if path == "/api/log":
            n = int((qs.get("lines") or ["120"])[0])
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
            save_settings(path, cur)
            self.bus.publish()
            self._json({"ok": True, "settings": cur})
            return

        if u.path == "/api/retry":
            ok = self.bus.retry(str(body.get("id") or ""))
            self._json({"ok": ok})
            return

        if u.path == "/api/delete":
            self._json({"ok": self._delete(body)})
            return

        self._json({"error": "not found"}, 404)

    def _delete(self, body: dict) -> bool:
        """从面板删掉一条记录: 删 mp4/图文文件 + _meta + .state.json 条目 + 缩略图。"""
        aid = str(body.get("id") or "")
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
                    d = t.parent / "_meta" / f"{aid}.json"
                    if d.is_file():
                        d.unlink()
                removed = True
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
            if what == "douyin":
                os.startfile("https://www.douyin.com/")      # noqa: S606
                return True
            if what == "file":
                aid = str(body.get("id") or "")
                for x in self.bus._recent():
                    if x["id"] == aid and x.get("file"):
                        target = self.bus.workspace / x["file"]
                        if target.is_dir():
                            os.startfile(str(target))         # noqa: S606
                        else:
                            subprocess.Popen(["explorer", "/select,", str(target)])
                        return True
            if what == "reveal":
                aid = str(body.get("id") or "")
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
    """起面板服务, 返回访问地址。端口被占就往后找几个。"""
    _Handler.bus = bus
    _Handler.web_file = web_file
    server = None
    last_err = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), _Handler)
            port = p
            break
        except OSError as e:
            last_err = e
            continue
    if server is None:
        log(f"  ! 面板端口被占用, 起不来: {last_err}")
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
