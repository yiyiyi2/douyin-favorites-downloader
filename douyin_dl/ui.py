# -*- coding: utf-8 -*-
"""
本地 Web 面板: 实时看下载进度、历史、设置。

为什么不引框架: 这套东西只跑在本机 127.0.0.1, 用标准库的
ThreadingHTTPServer + SSE 就够了, 不给用户装额外依赖。
"""

from __future__ import annotations

import json
import os
import stat as _stat
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

_SETTINGS_DEFAULTS = {
    "quality": "max", "open_browser": True, "auto_reopen": True,
    "by_author": False, "auto_rec": False, "auto_gap": 3.0,
    "auto_adv": True,
    # v2.2: 分段并发下载的连接数。CDN 限的是单连接速率, 不是账号速率,
    # 实测 8 路并发比单连接快 6.8 倍。0/1 = 关掉并发(老行为)。
    "dl_workers": 8,
}

# 设置文件读-改-写必须是原子的: 面板里快速连点两个开关, 两个 POST 各读各的
# 再各写整份, 会互相覆盖; write_text 写到一半崩了还会留下半个坏 JSON
# (正好触发 load_settings 的各种类型崩溃)。同一把锁 + 临时文件 os.replace。
_settings_lock = threading.RLock()
_settings_cache: dict[tuple, dict] = {}


def _as_dict(v) -> dict:
    """不是 dict 就当空 dict —— 防止畸形 JSON 让 .get/.items 抛 AttributeError。"""
    return v if isinstance(v, dict) else {}


def _as_number(v, default: float = 0.0) -> float:
    """把任意值变成有限浮点数; 容器 / NaN / Inf / 非数字字符串一律回落 default。"""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except Exception:
            return default
    else:
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _as_bool(v, default: bool = False) -> bool:
    """字符串 'false'/'0' 也算关, 'true'/'1' 算开; 其它类型回落 default。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        return default
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return bool(v)
    return default


def _rel_path(workspace: Path, rel) -> Path | None:
    """把 .state.json 里的 file 字段安全地变成 Path。

    v1.6: 以前直接 `workspace / item["file"]`, file 变成 int/dict 就 TypeError,
    相对路径 `../../etc/passwd` 还能被「打开/定位/删除」摸到工作区外面。
    现在非字符串一律拒绝; 相对路径必须留在工作区内; 绝对路径(用户自定义
    下载目录时 state 里存的就是绝对路径)放行。

    v2.3 性能: 这里原来对每条记录都 `workspace.resolve()` + `(root/p).resolve()`,
    一次 resolve 在 Windows 上要走 `nt._getfinalpathname` 系统调用。443 条记录
    就是 886 次系统调用、实测 _recent() 冷启动 944ms 里有 694ms 花在这。
    改成纯字符串判定(不碰文件系统): 先把 `rel` 规范化成用 "/" 分段, 出现
    ".." 段就直接判越界 —— 因为只要没有 "..", 相对路径就必然落在 root 下面。
    256 次抽样对比过新旧实现, 结论完全一致。
    """
    if not isinstance(rel, str) or not rel.strip():
        return None
    try:
        p = Path(rel)
        if p.is_absolute():
            return p
        # 纯字符串层拦路径穿越: 有 ".." 段才可能跑到工作区外面
        parts = [seg for seg in rel.replace("\\", "/").split("/") if seg not in ("", ".")]
        if ".." in parts:
            return None
        return workspace / Path(*parts) if parts else None
    except Exception:
        return None


def _safe_id(v) -> str:
    """id 会被拼进文件路径, 这里统一拦一下, 免得被 ../ 之类的东西穿越出去。

    v1.7: 抖音 aweme_id 是纯数字, 快手 photoId 是字母数字混排
    (如 3xb42hegyea3mzc)。所以放宽成 ASCII 字母数字 + _ -, 但依然
    只放行这一个白名单字符集 —— 斜杠、反斜杠、点、空格、中文一律挡掉,
    路径穿越防护一点没松。
    """
    s = str(v or "").strip()
    if not (5 <= len(s) <= 64):
        return ""
    if not all(c.isascii() and (c.isalnum() or c in "_-") for c in s):
        return ""
    return s


def _clamp_gap(v) -> float:
    """自动翻页间隔: 1~60 秒, 非法值/NaN/Inf 回落到 3 秒。"""
    try:
        g = _as_number(v, 3.0)
    except Exception:
        return 3.0
    return max(1.0, min(60.0, g))


def _clamp_workers(v) -> int:
    """分段并发连接数: 0~16。0/1 表示关掉并发(退回单连接)。"""
    try:
        n = int(_as_number(v, 8))
    except Exception:
        return 8
    return max(0, min(16, n))


def _clean_settings(data) -> dict:
    """把落盘/前端传来的设置强制归一化, 保证每个字段都是能用的类型。

    v1.6: 以前 `data.get("quality") not in QUALITY_CHOICES` 在 quality 是
    list/dict 时抛 `TypeError: unhashable type`, 一个坏 settings 就能让
    /api/state 与 /api/settings 全 500。这里所有字段都过一遍, 面板永远
    拿得到一个可用快照。
    """
    d = dict(_SETTINGS_DEFAULTS)
    d.update(_as_dict(data))
    q = d.get("quality")
    if not isinstance(q, str) or q not in QUALITY_CHOICES:
        d["quality"] = "max"
    for k in ("open_browser", "auto_reopen", "by_author", "auto_rec", "auto_adv"):
        d[k] = _as_bool(d.get(k), bool(_SETTINGS_DEFAULTS[k]))
    d["auto_gap"] = _clamp_gap(d.get("auto_gap"))
    d["dl_workers"] = _clamp_workers(d.get("dl_workers"))
    od = d.get("out_dir")
    if od is not None and not isinstance(od, str):
        od = None          # list/dict/int 都是坏值, 直接丢弃而不是 str() 出一串垃圾路径
    if isinstance(od, str) and not od.strip():
        od = None
    if od is None:
        d.pop("out_dir", None)
    else:
        d["out_dir"] = od.strip()
    return d


def load_settings(path: Path) -> dict:
    """读设置。带一层以 (mtime, size) 为键的缓存: 面板每秒推很多次快照,
    每次都重新读盘+解析 JSON 纯属浪费; 文件一变(面板保存/手工编辑)缓存自动失效。"""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except Exception:
        key = (str(path), -1, -1)
    with _settings_lock:
        cached = _settings_cache.get(key)
        if cached is not None:
            return dict(cached)
        data = dict(_SETTINGS_DEFAULTS)
        try:
            if path.exists():
                data.update(_as_dict(json.loads(path.read_text(encoding="utf-8"))))
        except Exception:
            pass
        clean = _clean_settings(data)
        if len(_settings_cache) > 32:
            _settings_cache.clear()
        _settings_cache[key] = clean
        return dict(clean)


def save_settings(path: Path, data: dict):
    """原子写: 同目录临时文件 -> os.replace, 不留半截坏 JSON。"""
    clean = _clean_settings(data)
    with _settings_lock:
        _settings_cache.clear()
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(str(tmp), str(path))
        except Exception:
            try:
                path.write_text(json.dumps(clean, ensure_ascii=False, indent=1),
                                encoding="utf-8")
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
        self.on_auto_gap = None                     # callable(float), 由主程序注入(实时改自动翻页间隔)
        self.on_auto_adv = None                     # callable(bool), 由主程序注入(实时开关自动翻页)
        self.on_out_dir = None                      # callable(Path), 由主程序注入(实时改下载目录全局)
        self.status = {"running": False, "browser": "", "note": ""}
        self.started = time.time()
        self.max_speed = 0.0
        self._recent_cache: list[dict] | None = None
        self._recent_sig: tuple | None = None
        self._meta_count: dict[str, tuple[int, int]] = {}
        self._dir_list: dict[str, tuple[int, dict[str, tuple[int, bool]]]] = {}
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
            for q in self.clients:
                try:
                    q.put_nowait(snap)
                except queue.Full:
                    # 旧行为: 队列满就把客户端移出 clients, 但 SSE 线程毫不知情,
                    # 仍每 10s 发 ping -> 浏览器以为在线却永远收不到 data, 界面静默冻结。
                    # 改成丢掉最旧一条, 保留订阅, 让慢客户端至少能收到最新快照。
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(snap)
                    except queue.Full:
                        pass

    # -- 写入 -------------------------------------------------------------
    def set_status(self, **kw):
        with self.lock:
            self.status.update(kw)
        self.publish()

    def add_waiting(self, aid: str, title: str = "", author: str = "", source: str = ""):
        changed = False
        with self.lock:
            # v1.6.3: 这条又进队列了(重新收藏 / 重试成功 / 解析中), 说明它正在
            # 重新走流程, 失败列表里那条旧记录必须立刻摘掉。否则用户重新收藏
            # 成功后, 面板上"下载失败"还挂着同一条 —— 看着像根本没修好。
            if self.failed.pop(str(aid), None) is not None:
                changed = True
            for x in self.waiting:
                if x.get("id") != aid:
                    continue
                # v1.6.2: 收藏那一刻先插一条占位(source="解析中"), 详情抓回来
                # 后再调一次 add_waiting。旧版直接 return, 占位的空标题/「解析中」
                # 就永远留在队列里了 —— 现在用后来的信息把它补全。
                if title and not x.get("title"):
                    x["title"] = title
                    changed = True
                if author and not x.get("author"):
                    x["author"] = author
                    changed = True
                if source and source != "解析中" and x.get("source") != source:
                    x["source"] = source
                    changed = True
                break
            else:
                self.waiting.append({"id": aid, "title": title, "author": author,
                                     "source": source, "at": int(time.time())})
                if len(self.waiting) > 500:          # 长期挂机别让队列无限涨
                    del self.waiting[:-500]
                changed = True
        if changed:
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
            # v2.3: 缓存和签名都不动。_recent() 会自己比对 .state.json 的
            # 元信息签名 —— 文件真变了才重解析。这里清任何一样都会让面板
            # 每下完一个视频就全量重扫 443 条(实测 ~50ms 的卡顿)。
            # (落盘由 state.mark() 负责, 它写完文件, 签名自然就变了)
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
        """把失败的那条重新交给下载器。

        v1.6: 改成后台线程受理, POST /api/retry 不再等 refetch(最多 30 秒),
        整个面板不会被冻住。
        v1.6.2: 受理后**立刻**把这行标成 retrying 并推送。旧版虽然也是异步,
        但失败项要等无头浏览器抓完(5~45 秒)才第一次变化, 用户盯着看就是
        「点了没反应」, 于是连点好几下。现在点下去一百毫秒内就有反馈;
        抓取失败时把具体原因写回 why, 用户知道为什么还是不行。
        """
        fn = self.on_retry
        if not fn:
            return False
        aid = str(aid)
        with self.lock:
            rec = self.failed.get(aid)
            if not isinstance(rec, dict):
                return False
            if rec.get("retrying"):
                return True                  # 已经在抓了, 别叠加请求
            rec["retrying"] = True
            rec["time"] = int(time.time())
        self.publish()

        def _work():
            ok = False
            why = ""
            try:
                res = fn(aid)
                if isinstance(res, tuple):
                    ok = bool(res[0])
                    why = str(res[1] or "") if len(res) > 1 else ""
                else:
                    ok = bool(res)
            except Exception as e:
                why = f"重试异常: {str(e)[:60]}"
            with self.lock:
                rec2 = self.failed.get(aid)
                if isinstance(rec2, dict):
                    if ok:
                        self.failed.pop(aid, None)
                    else:
                        rec2["retrying"] = False
                        base = str(rec2.get("why") or "")
                        add = why or "重试仍失败"
                        # 别把同一句原因叠成 "xxx · xxx · xxx"
                        if add not in base:
                            base = (base + " · " + add).strip(" ·")
                        rec2["why"] = base
                        rec2["time"] = int(time.time())
            self.publish()

        threading.Thread(target=_work, name=f"dy-ui-retry-{aid[:12]}", daemon=True).start()
        return True

    def dismiss(self, aid: str) -> bool:
        """从面板上叉掉一条失败记录。

        只动内存里的失败清单, 不碰已下载的文件、不动 .state.json —— 用户
        要的是「别在我眼前挂着」, 不是把下载好的东西删了。
        """
        with self.lock:
            gone = self.failed.pop(str(aid), None) is not None
        if gone:
            self.publish()
        return gone

    def set_speed(self, mbps: float):
        with self.lock:
            if mbps > self.max_speed:
                self.max_speed = round(mbps, 1)

    def _lookup_file(self, f: Path, checked: set[str] | None = None) -> tuple[int, bool] | None:
        """查一个文件是否存在、多大、是不是目录 —— 用目录清单, 不逐条 stat。

        返回 (大小, 是否目录); 不存在返回 None。

        `checked` 是一次 _recent() 调用内的"这个目录本帧已验过 mtime"集合。
        同一次调用里 443 条记录绝大多数落在同一个目录, 逐条去 stat 目录
        就是 443 次系统调用 —— 有了它只 stat 一次。
        """
        parent = f.parent
        key = str(parent).lower()
        cached = self._dir_list.get(key)
        if checked is not None and key in checked:
            return cached[1].get(f.name) if cached else None
        try:
            ms = parent.stat()
            mt = ms.st_mtime_ns
        except OSError:
            self._dir_list.pop(key, None)
            return None
        if checked is not None:
            checked.add(key)
        if cached is None or cached[0] != mt:
            listing: dict[str, tuple[int, bool]] = {}
            try:
                with os.scandir(parent) as it:
                    for e in it:
                        try:
                            isdir = e.is_dir()
                            size = 0 if isdir else e.stat().st_size
                            listing[e.name] = (size, isdir)
                        except OSError:
                            continue
            except OSError:
                return None
            self._dir_list[key] = (mt, listing)
            cached = (mt, listing)
        return cached[1].get(f.name)

    def _state_sig(self) -> tuple:
        """判断历史记录是否真的需要重新解析 —— 只看文件元信息, 不读内容。

        v2.3: 原来缓存只有"5 秒 TTL"。但每次 finish()/fail()/delete() 都会把
        _recent_cache 置 None, 于是下一次 snapshot() 必然全量重扫 443 条记录,
        每条还要 exists()+stat() 两次系统调用。一次下载完成就会让面板卡一下。
        现在改成"元信息签名": .state.json 的 (mtime, size) + _meta 目录的
        (mtime, 文件数)。签名没变就直接用缓存 —— 无论 TTL 过没过。
        """
        try:
            st = self.state_file.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except Exception:
            sig = (0, 0)
        metas = []
        seen_dirs = set()
        for d in (self.out_dir / "_meta", self.workspace / "downloads" / "_meta"):
            # 默认下载目录时这两条是同一个目录, 扫两遍纯属浪费
            key = str(d).lower()
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            try:
                ms = d.stat()
                mk = str(d).lower()
                # 目录 mtime 变了才重新数文件数 —— 新增/删除 _meta 文件必然
                # 改动目录 mtime; 内容改写则由 .state.json 的签名兜住
                # (_save_meta 之后紧接着 state.mark, 两者都会动)。
                # 这样稳态下只剩 2 次 stat, 不再 scandir 上百个目录项。
                if mk in self._meta_count and self._meta_count[mk][0] == ms.st_mtime_ns:
                    n = self._meta_count[mk][1]
                else:
                    n = 0
                    with os.scandir(d) as it:
                        for e in it:
                            if e.name.endswith(".json"):
                                n += 1
                    self._meta_count[mk] = (ms.st_mtime_ns, n)
                metas.append((str(d), ms.st_mtime_ns, n))
            except Exception:
                metas.append((str(d), 0, 0))
        return (sig, tuple(metas))

    # -- 读取 -------------------------------------------------------------
    def _recent(self) -> list[dict]:
        """历史记录 = .state.json + downloads/_meta/*.json。

        v1.6: .state.json 是落盘文件, 可能被旧版写成别的形状 / 被手工编辑 /
        写到一半断电。以前 json.loads 之外的访问全裸奔, 一个坏值就让
        /api/state 与所有 SSE 推送一起 500。现在每一行都做类型归一, 坏行跳过,
        整段再兜一层, 保证面板永远能出得来。

        v2.3: 缓存键从"5 秒 TTL"改成"文件元信息签名"(_state_sig)。仅元信息
        就变了才重新解析, 否则无脑复用。原来 finish() 后清缓存 + TTL 过期都会
        触发全量重扫(实测冷启动 944ms), 现在稳态是几微秒。
        """
        sig = self._state_sig()
        if self._recent_cache is not None and sig == self._recent_sig:
            return self._recent_cache
        out: list[dict] = []
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
            if not isinstance(state, dict):
                state = {}
            meta_dir = self.out_dir / "_meta"
            meta_dir_default = self.workspace / "downloads" / "_meta"   # 改过下载目录后老元数据还在默认位置
            # v2.3: 先用 scandir 给每个 _meta 目录建一份"文件名 -> 路径"索引。
            # 原来每条记录对每个目录都 mp.exists() —— 443 条 x 2 个目录 = 886 次
            # stat, 实测占 _recent() 的 70%。目录项早就全在 scandir 结果里了。
            meta_index: list[dict[str, str]] = []
            seen_md = set()
            for md in (meta_dir, meta_dir_default):
                key = str(md).lower()
                if key in seen_md:
                    continue
                seen_md.add(key)
                idx: dict[str, str] = {}
                try:
                    with os.scandir(md) as it:
                        for e in it:
                            if e.name.endswith(".json"):
                                idx[e.name[:-5]] = e.path
                except OSError:
                    pass
                meta_index.append(idx)
            if not meta_index:
                meta_index = [{}]
            checked_dirs: set[str] = set()     # 本帧已验过 mtime 的目录
            for aid, row in state.items():
                if not isinstance(row, dict):
                    row = {}
                f = _rel_path(self.workspace, row.get("file"))
                tm = _as_number(row.get("time", 0), 0.0)
                item = {
                    "id": str(aid),
                    "file": row.get("file") if isinstance(row.get("file"), str) else "",
                    "quality": row.get("quality") if isinstance(row.get("quality"), str) else "",
                    "time": tm,
                    "title": "",
                    "author": "",
                    "cover": "",
                    "size": 0,
                    "type": "video",
                }
                if f is not None:
                    # v2.3: 原来 exists()+stat()+is_dir() 三次系统调用, 443 条就是
                    # 1300+ 次、实测 200ms。现在先 scandir 一次把整个目录的名字/
                    # 大小/类型全捞出来(目录 mtime 变了才重捞), 再按名字查表。
                    # 绝大多数记录的文件早就被用户删了, 逐条 stat 全是白走。
                    info = self._lookup_file(f, checked_dirs)
                    if info is not None:
                        sz, isdir = info
                        if isdir:
                            item["type"] = "images"
                        else:
                            item["size"] = sz
                safe = _safe_id(aid)
                for idx in meta_index:
                    if not safe:
                        break
                    mp = idx.get(safe)
                    if not mp:
                        continue
                    try:
                        m = json.loads(Path(mp).read_text(encoding="utf-8"))
                        if not isinstance(m, dict):
                            continue
                        item["title"] = m.get("desc", "") if isinstance(m.get("desc"), str) else ""
                        item["author"] = m.get("author", "") if isinstance(m.get("author"), str) else ""
                        item["cover"] = m.get("cover", "") if isinstance(m.get("cover"), str) else ""
                        item["duration"] = _as_number(m.get("duration_ms", 0), 0)
                        item["music"] = m.get("music", "") if isinstance(m.get("music"), str) else ""
                        break
                    except Exception:
                        continue
                out.append(item)
        except Exception:
            out = []
        out.sort(key=lambda x: x.get("time") or 0, reverse=True)
        self._recent_cache = out
        self._recent_sig = sig
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
            "today": sum(1 for x in recent if _as_number(x.get("time"), 0.0) >= today0),
            "bytes": sum(_as_number(x.get("size"), 0.0) for x in recent),
            "max_speed": self.max_speed,
            "quality": conf.get("quality") or "max",
            "quality_choices": QUALITY_CHOICES,
            "out_dir": str(self.out_dir),
            "by_author": bool(conf.get("by_author")),
            "auto_reopen": bool(conf.get("auto_reopen", True)),
            "auto_rec": bool(conf.get("auto_rec", False)),
            "auto_gap": _clamp_gap(conf.get("auto_gap")),
            "auto_adv": bool(conf.get("auto_adv", True)),
            "out_dir_configured": bool(conf.get("out_dir")),
            # v2.2: 分段并发下载的连接数。0/1 = 关掉并发走单连接。
            "dl_workers": _clamp_workers(conf.get("dl_workers")),
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

    # -- 离线播放器用到的两个东西 -------------------------------------------
    def library(self) -> list[dict]:
        """给离线播放器用的视频库 = _recent() 里"文件确实在"的那些。

        只多给播放器真正需要的字段, 不把 _recent 的全套都甩出去。
        `src` 是 /api/video?id=<aid> 而不是磁盘路径 —— 浏览器拿不到本地
        路径, 而且这样服务端还能统一做 Range 和路径校验。
        """
        out: list[dict] = []
        for x in self._recent():
            if x.get("type") != "video":
                continue
            sz = x.get("size") or 0
            if sz <= 0:
                continue                      # 文件早被删了, 别放进播放列表
            aid = x.get("id") or ""
            if not aid:
                continue
            out.append({
                "id": aid,
                "src": f"/api/video?id={aid}",
                "title": x.get("title") or "",
                "author": x.get("author") or "",
                "music": x.get("music") or "",
                "quality": x.get("quality") or "",
                "duration": x.get("duration") or 0,
                "size": sz,
                "time": x.get("time") or 0,
                "has_thumb": (self.thumbs_dir / f"{aid}.jpg").exists(),
            })
        return out

    def video_path(self, aid: str) -> Path | None:
        """aid -> 磁盘上的 mp4 路径。只认 .state.json 里登记过、且真在磁盘上的。"""
        aid = _safe_id(aid)
        if not aid:
            return None
        for x in self._recent():
            if x.get("id") != aid or x.get("type") != "video":
                continue
            p = _rel_path(self.workspace, x.get("file"))
            if p is None:
                return None
            # 二次确认没跑出工作区(符号链接/短路径都可能绕过纯字符串判定)
            try:
                if not p.is_file() or not str(p.resolve()).startswith(str(self.workspace.resolve())):
                    return None
            except OSError:
                return None
            return p
        return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    bus: EventBus = None          # type: ignore
    web_file: Path = None         # type: ignore
    _player_file: Path = None     # type: ignore  # 离线播放器页面
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
        try:
            self._route_get()
        except Exception as e:
            try:
                self._json({"error": f"请求处理失败: {e}"}, 500)
            except Exception:
                pass

    def _serve_video(self):
        """把 mp4 流式发给浏览器, 支持 Range —— 进度条才能拖动。

        不用 SimpleHTTPRequestHandler 是因为这里要按 aid 白名单取路径,
        不能让请求里的路径直接决定读哪个文件。
        """
        u = urlparse(self.path)
        aid = _safe_id((parse_qs(u.query).get("id") or [""])[0])
        p = self.bus.video_path(aid) if aid else None
        if p is None:
            self._json({"error": "视频不存在或已被删除"}, 404)
            return

        try:
            total = p.stat().st_size
        except OSError:
            self._json({"error": "文件读不到"}, 500)
            return

        ctype = "video/mp4"
        rng = (self.headers.get("Range") or "").strip()
        start, end = 0, total - 1
        partial = False
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a == "" and b != "":                 # bytes=-N  (最后 N 字节)
                    n = int(b)
                    if n > 0:
                        start, partial = max(0, total - n), True
                elif a != "":
                    start, partial = max(0, int(a)), True
                    if b != "":
                        end = min(int(b), total - 1)
            except ValueError:
                start, end, partial = 0, total - 1, False

        if partial and start >= total:
            # 越界 Range 按 RFC 该回 416, 并告诉对方真实长度
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        n = end - start + 1
        try:
            f = open(p, "rb")
        except OSError:
            self._json({"error": "文件读不到"}, 500)
            return
        try:
            f.seek(start)
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(n))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            # 视频文件不会变, 但换个同名文件也得能播, 用长度+mtime 当弱校验
            try:
                self.send_header("ETag", f'"{total}-{int(p.stat().st_mtime)}"')
            except OSError:
                pass
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            left = n
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass                              # 用户拖进度条/切视频, 正常现象
        finally:
            f.close()

    def _route_get(self):
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

        # -- 离线播放器 ---------------------------------------------------
        if path in ("/player", "/player.html", "/play"):
            try:
                html = self._player_file.read_bytes()
            except Exception as e:
                self._json({"error": f"播放器页面读不到: {e}"}, 500)
                return
            self._bytes(html, "text/html; charset=utf-8", cache=False)
            return

        if path == "/api/library":
            self._json({"items": self.bus.library()})
            return

        if path == "/api/video":
            self._serve_video()
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
        try:
            u = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            # v1.6: 不设上限的话, 一个 `Content-Length: 999999999999` 就能
            # 让 rfile.read 吃光内存 / 挂住这个请求线程; 负数会读到连接关闭。
            if length < 0 or length > (1 << 20):
                self._json({"ok": False, "error": "请求体太大或长度非法"}, 400)
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            try:
                self._route_post(u.path, body)
            except Exception as e:
                # v1.6: 结构错的 body / 坏 URL / 任何 handler 异常都必须给客户端一个
                # 明确响应, 不能让连接被重置, 也不能让 ThreadingHTTPServer 吞掉堆栈。
                try:
                    self._json({"ok": False, "error": f"请求处理失败: {e}"}, 500)
                except Exception:
                    pass
        except Exception:
            # 连 urlparse 都可能抛(畸形 IPv6 URL), 兜到底
            try:
                self._json({"ok": False, "error": "请求无效"}, 400)
            except Exception:
                pass

    def _route_post(self, path: str, body: dict):
        if path == "/api/open":
            self._json({"ok": self._open(body)})
            return

        if path == "/api/settings":
            settings_path = self.bus.workspace / ".settings.json"
            with _settings_lock:
                cur = load_settings(settings_path)
                q = body.get("quality")
                if isinstance(q, str) and q in QUALITY_CHOICES:
                    cur["quality"] = q
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
                # v1.5: 自动翻页间隔(秒)。1~60 之外一律夹回去, 避免 0 秒疯狂翻页把页面刷崩。
                if "auto_gap" in body:
                    g = _clamp_gap(body.get("auto_gap"))
                    cur["auto_gap"] = g
                    cb = getattr(self.bus, "on_auto_gap", None)
                    if callable(cb):
                        try:
                            cb(g)
                        except Exception:
                            pass
                # v1.5: 自动翻页开关(自动下推荐页开着时才有效)
                if isinstance(body.get("auto_adv"), bool):
                    cur["auto_adv"] = body["auto_adv"]
                    cb = getattr(self.bus, "on_auto_adv", None)
                    if callable(cb):
                        try:
                            cb(body["auto_adv"])
                        except Exception:
                            pass
                # v2.2: 下载连接数(分段并发)。0/1 = 关掉并发走单连接。
                # 只改数字, 不重启也立即生效 —— Downloader 每次下载都重新读。
                if "dl_workers" in body:
                    cur["dl_workers"] = _clamp_workers(body.get("dl_workers"))
                # 下载目录可调: 必须是字符串; 相对路径按工作区补全后存。
                # 建不出来(盘不存在/没权限)就拒绝, 不留下一个下载会悄悄失败的设置。
                if "out_dir" in body:
                    od = body.get("out_dir")
                    raw = od.strip() if isinstance(od, str) else ""
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
                    cb = getattr(self.bus, "on_out_dir", None)
                    if callable(cb):
                        try:
                            cb(self.bus.out_dir)
                        except Exception:
                            pass
                save_settings(settings_path, cur)
            self.bus.publish()
            self._json({"ok": True, "settings": cur})
            return

        if path == "/api/retry":
            ok = self.bus.retry(_safe_id(body.get("id")))
            self._json({"ok": ok})
            return

        if path == "/api/fail/dismiss":
            # 只把这条从失败清单里叉掉, 不动文件、不动下载记录
            self._json({"ok": self.bus.dismiss(_safe_id(body.get("id")))})
            return

        if path == "/api/delete":
            self._json({"ok": self._delete(body)})
            return

        self._json({"error": "not found"}, 404)

    def _delete(self, body: dict) -> bool:
        """从面板删掉一条记录: 删 mp4/图文文件 + _meta + .state.json 条目 + 缩略图。

        v1.6: 返回值只取决于「state 里还有没有这条」, 文件已经不在也照样算删掉,
        重复点删除不会弹「删除失败」。
        """
        aid = _safe_id(body.get("id"))
        if not aid:
            return False
        try:
            for x in self.bus._recent():
                if x.get("id") != aid:
                    continue
                f = _rel_path(self.bus.workspace, x.get("file"))
                if f and f.is_file():
                    try:
                        f.unlink()
                    except Exception:
                        pass
                elif f and f.is_dir():
                    try:
                        import shutil
                        shutil.rmtree(f, ignore_errors=True)
                    except Exception:
                        pass
                # meta 写在下载根 _meta/ (main 的 _save_meta), 不是文件旁边;
                # 按作者分文件夹时两者不在同一目录, 所以两个位置都找一遍。
                for md in (self.bus.out_dir / "_meta",
                           self.bus.workspace / "downloads" / "_meta"):
                    try:
                        mp = md / f"{aid}.json"
                        if mp.is_file():
                            mp.unlink()
                    except Exception:
                        pass
                    try:
                        if f:
                            mp2 = f.parent / "_meta" / f"{aid}.json"
                            if mp2.is_file():
                                mp2.unlink()
                    except Exception:
                        pass
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
                    try:
                        data = json.loads(st.read_text(encoding="utf-8"))
                        if isinstance(data, dict) and aid in data:
                            data.pop(aid, None)
                            st.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
            th = self.bus.thumbs_dir / f"{aid}.jpg"
            if th.exists():
                try:
                    th.unlink()
                except Exception:
                    pass
            self.bus._recent_cache = None
            self.bus.publish()
        except Exception:
            pass
        return True

    def _open(self, body: dict) -> bool:
        what = body.get("what")
        try:
            if what == "folder":
                os.startfile(str(self.bus.out_dir))          # noqa: S606
                return True
            if what == "file":
                aid = _safe_id(body.get("id"))
                for x in self.bus._recent():
                    if x["id"] == aid:
                        target = _rel_path(self.bus.workspace, x.get("file"))
                        if not target or not target.exists():
                            continue
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
                    if x["id"] == aid:
                        target = _rel_path(self.bus.workspace, x.get("file"))
                        if not target or not target.exists():
                            continue
                        if target.is_file():
                            subprocess.Popen(
                                ["explorer", "/select,", str(target)],
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        else:
                            os.startfile(str(target))         # noqa: S606
                        return True
            if what == "log":
                os.startfile(str(self.bus.log_file))          # noqa: S606
                return True
            if what == "site":
                return self._open_site(body)
        except Exception:
            return False
        return False

    # v2.1: 面板上的「平台选择」—— 点一下就打开对应站点。
    # 走 CDP 在**用户正在用的那个 Chrome**里新开标签页, 不另起浏览器进程,
    # 所以登录态、已装的钩子、正在刷的页面全都连续。
    _SITES = {
        "douyin": "https://www.douyin.com/",
        "kuaishou": "https://www.kuaishou.com/new-reco",
        "bilibili": "https://www.bilibili.com/",
    }

    def _open_site(self, body: dict) -> bool:
        which = str(body.get("site") or "").strip().lower()
        url = self._SITES.get(which)
        if not url:
            return False
        port = int(body.get("port") or 9222)
        cb = getattr(self.bus, "on_open_site", None)
        if callable(cb):
            try:
                if cb(which, url):
                    return True
            except Exception as e:      # noqa: BLE001
                # 别静默吞: 回调出错时记一笔, 否则面板只看到 "操作失败"
                try:
                    with open(self.bus.log_file, "a", encoding="utf-8") as _f:
                        _f.write(f"  ! 打开 {which} 回调异常: {str(e)[:80]}\n")
                except Exception:
                    pass
        # 兜底: 自己连 CDP 开标签页。
        # 注意**不能**在这里用 sync_playwright —— 主进程已经在主 greenlet 里
        # 跑了一个 Playwright, 面板请求在 dy-ui 线程里再起一个会互相干扰、
        # 而且会把这个 HTTP 请求一直挂住(实测卡死 120 秒以上)。
        # 这里走纯 CDP websocket: 一条 Target.createTarget 就够, 线程安全。
        return self._open_site_raw(port, url)

    def _open_site_raw(self, port: int, url: str) -> bool:
        try:
            import json as _json
            import urllib.request as _ur
            import websocket
            with _ur.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
                ws_url = _json.loads(r.read()).get("webSocketDebuggerUrl")
            if not ws_url:
                return False
            conn = websocket.create_connection(ws_url, timeout=8,
                                               enable_multithread=False)
            try:
                conn.send(_json.dumps({
                    "id": 1, "method": "Target.createTarget",
                    "params": {"url": url, "background": False},
                }))
                while True:
                    msg = _json.loads(conn.recv())
                    if msg.get("id") == 1:
                        return bool((msg.get("result") or {}).get("targetId"))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            return False


def start_ui(bus: EventBus, web_file: Path, port: int = 8899,
             open_browser: bool = True, log=print) -> str:
    """起面板服务, 返回访问地址。

    v1.6: 旧行为是探测到 8899 已有面板就 `return ""`, 本进程继续下载却不
    起服务 —— 一旦那个实例退出, 端口上就再没有面板, 用户标签页永远断线,
    而且注释里那句「SSE 会自动重连到新进程」根本不成立(新进程没服务)。
    现在改成看门狗: 端口被占时先盯着, 一空出来就由本进程把服务补上。
    """
    _Handler.bus = bus
    _Handler.web_file = web_file
    # 播放器页面跟主面板放同一个 web 目录; 找不到时退化成主面板(总比 500 好)
    _p = Path(web_file).parent / "player.html"
    _Handler._player_file = _p if _p.exists() else Path(web_file)
    url = f"http://127.0.0.1:{port}/"

    def _probe_state() -> bool:
        try:
            import urllib.request as _ur
            with _ur.urlopen(url + "api/state", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def _bind():
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        except OSError:
            return None

    def _serve(server):
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="dy-ui").start()

    def _open_browser():
        time.sleep(0.8)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _watchdog(reason: str):
        log(f"  {reason}; 端口空出后本进程会自动接管面板")
        while True:
            time.sleep(2)
            if _probe_state():
                continue                      # 对方面板还活着, 继续等
            s = _bind()
            if s is not None:
                log(f"  已接管面板: {url}")
                _serve(s)
                return
            # 绑不上(别的非面板程序占着/ TIME_WAIT), 下一轮再试

    if _probe_state():
        threading.Thread(target=_watchdog, name="dy-ui-watch", daemon=True,
                         args=(f"面板已在运行: {url} (不重复打开)",)).start()
        return url

    server = _bind()
    if server is None:
        threading.Thread(target=_watchdog, name="dy-ui-watch", daemon=True,
                         args=(f"! 面板端口 {port} 被其他程序占用",)).start()
        return url

    _serve(server)
    if open_browser:
        threading.Thread(target=_open_browser, daemon=True).start()
    return url
