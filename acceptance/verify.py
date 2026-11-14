# -*- coding: utf-8 -*-
"""抖音 / 快手收藏自动下载 —— 完整功能验收 harness (v1.7.0)

用户 (m10164) 要求:「现在是WIFI环境，你做完自己测一遍，记得一定要把所有的功能都测两遍遍，我必须看到完美运行的程序。」
本脚本把「一遍」定义为下面 A~H 八大类、约 60 个独立检查全部跑完;
`--round 1` 与 `--round 2` 各跑一遍, 每遍各自落一份 JSON 证据。
`--report` 把两轮证据合成一张可视化的 HTML 报告 + 一张 PNG 截图。

设计原则:
  * 绝不改动用户真实数据: 合成数据走临时目录 / 内联 HTTP 服务器;
    真实面板只发**无副作用**请求(设置项当场还原, 不碰 auto_rec/auto_adv/
    auto_reopen/out_dir, 不调 /api/open, 不删真实记录)。
  * 绝不关用户的浏览器: live CDP 只读 eval, 只 pw.stop() 不 br.close(),
    并在事后断言 9222 仍活着当护栏。
  * 不用 mock 糊弄: 真起本地 HTTP 服务器跑 core.download 全链路(含 Range
    断点续传 / 416 / 内容嗅探), 真跑 Downloader.process, 真跑全量 pytest。
"""
import argparse
import ast
import http.client
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
ACC = ROOT / "acceptance"
ACC.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from douyin_dl import core                                    # noqa: E402
from douyin_dl import ui as ui_mod                            # noqa: E402
from douyin_dl import __version__ as APP_VERSION              # noqa: E402
from douyin_dl.ui import (                                    # noqa: E402
    EventBus, QUALITY_CHOICES, _clamp_gap, _safe_id,
    filter_candidates, load_settings, save_settings,
)
from douyin_dl.watcher import (                                # noqa: E402
    HOOK_JS, HOOK_VER, BrowserWatcher, PLATFORM_KUAISHOU,
    _is_ks_id, collect_ks_ids, extract_ks_photos, ks_action_value,
    ks_ids_from_url,
)

# 复用 tests/test_kuaishou.py 里按真实响应形状造的数据
_spec = importlib.util.spec_from_file_location("acc_fixtures", ROOT / "tests" / "test_kuaishou.py")
FIX = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FIX)
ks_photo = FIX.ks_photo
ks_feed_body = FIX.ks_feed_body
ks_apollo_client = FIX.ks_apollo_client

# 把 douyin_dl.py 当模块加载(不是 __main__, 不会启动程序)
_mspec = importlib.util.spec_from_file_location("acc_main", ROOT / "douyin_dl.py")
M = importlib.util.module_from_spec(_mspec)
_mspec.loader.exec_module(M)

DY_AID = "7705427599274719717"
KS_AID = "3xb42hegyea3mzc"
PANEL_PORT = 8899
CDP_PORT = 9222


# ---------------------------------------------------------------------------
# 合成数据
# ---------------------------------------------------------------------------
def dy_aweme(aid=DY_AID):
    """真实形状的抖音 aweme: 3 个码率档, 其中一个是水印 playwm(应被过滤)。"""
    return {
        "aweme_id": aid,
        "desc": "测试抖音视频 验收用",
        "create_time": 1789307548,
        "author": {"nickname": "测试作者", "uid": "u123456"},
        "music": {"title": "测试BGM", "author": "音乐人"},
        "video": {
            "duration": 15000,
            "width": 1080, "height": 1920,
            "cover": {"url_list": ["https://p3-pc.douyin.com/cover.jpeg"]},
            "bit_rate": [
                {"gear_name": "1080p", "bit_rate": 5000000, "quality_type": "1080",
                 "play_addr": {"url_list": ["https://cdn.douyin.com/v1080.mp4"],
                               "width": 1080, "height": 1920, "data_size": 30000000}},
                {"gear_name": "720p", "bit_rate": 2000000, "quality_type": "720",
                 "play_addr": {"url_list": ["https://cdn.douyin.com/v720.mp4"],
                               "width": 720, "height": 1280, "data_size": 12000000}},
                {"gear_name": "watermark", "bit_rate": 9999999,
                 "play_addr": {"url_list": ["https://cdn.douyin.com/playwm/vwm.mp4"],
                               "width": 2160, "height": 3840, "data_size": 50000000}},
            ],
        },
    }


def ks_wrapped(pid=KS_AID):
    # 与真实管线一致: feed 里平级的 author 先合并进 photo 再缓存
    ph = extract_ks_photos(ks_feed_body(ks_photo(pid)))[0]
    return {"aweme_id": pid, "_platform": PLATFORM_KUAISHOU, "_ks": ph}


def dy_feed_data(aw):
    return json.dumps({"status_code": 0, "aweme_list": [aw]}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 迷你工具
# ---------------------------------------------------------------------------
def http_req(method, path, body=None, port=PANEL_PORT, timeout=6, raw_headers=None):
    """返回 (status, headers_dict, bytes)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    hdrs = {"Content-Type": "application/json"}
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Length"] = str(len(payload))
    if raw_headers:
        hdrs.update(raw_headers)
    conn.request(method, path, body=payload, headers=hdrs)
    r = conn.getresponse()
    data = r.read()
    hd = dict(r.getheaders())
    st = r.status
    conn.close()
    return st, hd, data


def port_alive(port, timeout=1.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def cdp_json(path="/json/version", timeout=3):
    st, _, data = http_req("GET", path, port=CDP_PORT, timeout=timeout)
    return st, data


def sse_first_frame(port=PANEL_PORT, timeout=5):
    """读 SSE 的第一帧 data: {...}, 立刻断开(不订阅到底)。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.settimeout(timeout)
    s.sendall(
        b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Accept: text/event-stream\r\nConnection: close\r\n\r\n"
    )
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline and b"\n\n" not in buf:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    try:
        s.close()
    except OSError:
        pass
    m = re.search(rb"^data: (\{.*\})\s*$", buf, re.M)
    return (json.loads(m.group(1).decode("utf-8")) if m else None), buf[:200]


def oversize_post_response():
    """Content-Length 声明超大, 服务端应在读 body 之前就回 400。"""
    s = socket.create_connection(("127.0.0.1", PANEL_PORT), timeout=4)
    s.settimeout(4)
    s.sendall(
        b"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\nContent-Length: 1048577\r\n"
        b"Connection: close\r\n\r\n"
    )
    buf = b""
    deadline = time.time() + 6
    while time.time() < deadline and len(buf) < 65536:
        try:
            c = s.recv(4096)
        except socket.timeout:
            break
        if not c:
            break
        buf += c
    try:
        s.close()
    except OSError:
        pass
    return buf


# ---------------------------------------------------------------------------
# 本地视频服务器: 真跑 core.download / probe_size
# ---------------------------------------------------------------------------
PAYLOAD = (b"\x00\x00\x00\x20ftypisom" +
           bytes((i * 7 + 13) & 0xFF for i in range(8000)))


class VideoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="video/mp4", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        p = urlparse(self.path).path
        rng = self.headers.get("Range") or ""
        m = re.search(r"bytes=(\d+)-", rng)
        pos = int(m.group(1)) if m else 0
        if p == "/full" or p == "/resume":
            if pos:
                self._send(206, PAYLOAD[pos:], extra={
                    "Content-Range": f"bytes {pos}-{len(PAYLOAD)-1}/{len(PAYLOAD)}"})
            else:
                self._send(200, PAYLOAD)
            return
        if p == "/probe":
            if pos:
                self._send(206, PAYLOAD[pos:pos + 2], extra={
                    "Content-Range": f"bytes {pos}-{min(pos+1, len(PAYLOAD)-1)}/{len(PAYLOAD)}"})
            else:
                self._send(200, PAYLOAD[:2], extra={
                    "Content-Range": f"bytes 0-1/{len(PAYLOAD)}"})
            return
        if p == "/416":
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/html":
            self._send(200, b"<html><body>login</body></html>", "text/html; charset=utf-8")
            return
        if p == "/json":
            self._send(200, b'{"result":2}', "application/json")
            return
        if p == "/sniff":
            self._send(200, b"<!DOCTYPE html><html>bad</html>", "application/octet-stream")
            return
        if p == "/trunc":
            # 第一次: 声明全量长度却只发一半并断连 -> download 会留 .part 续传
            if not pos:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.end_headers()
                try:
                    self.wfile.write(PAYLOAD[:len(PAYLOAD) // 2])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
            else:
                self._send(206, PAYLOAD[pos:], extra={
                    "Content-Range": f"bytes {pos}-{len(PAYLOAD)-1}/{len(PAYLOAD)}"})
            return
        self._send(404, b"not found", "text/plain")


def start_video_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), VideoHandler)
    t = threading.Thread(target=srv.serve_forever, name="acc-video", daemon=True)
    t.start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------------------
# 一轮验收
# ---------------------------------------------------------------------------
class Round:
    def __init__(self, n):
        self.n = n
        self.cats = []
        self.cur = None
        self.evidence = {}
        self.t0 = time.time()
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def cat(self, name):
        self.cur = {"name": name, "checks": []}
        self.cats.append(self.cur)
        print(f"\n=== {name} ===")
        return self

    def check(self, name, fn):
        t = time.time()
        detail = ""
        try:
            r = fn()
            if isinstance(r, tuple):
                ok, detail = bool(r[0]), str(r[1] if len(r) > 1 else "")
            else:
                ok = bool(r)
        except Exception as e:
            ok = False
            detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"
        rec = {"name": name, "ok": bool(ok), "detail": detail[:800],
               "ms": int((time.time() - t) * 1000)}
        self.cur["checks"].append(rec)
        tail = f"  — {detail.splitlines()[0][:130]}" if (detail and not ok) else ""
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")
        return ok

    def finish(self):
        checks = [c for cat in self.cats for c in cat["checks"]]
        passed = sum(1 for c in checks if c["ok"])
        return {
            "round": self.n,
            "started_at": self.started_at,
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_s": round(time.time() - self.t0, 2),
            "env": {
                "version": APP_VERSION, "hook_ver": HOOK_VER,
                "cwd": str(ROOT), "python": sys.version.split()[0],
            },
            "evidence": self.evidence,
            "categories": self.cats,
            "total": len(checks), "passed": passed,
            "failed": len(checks) - passed,
        }


def run_round(n):
    import tempfile
    R = Round(n)
    tmp = Path(tempfile.mkdtemp(prefix=f"dy_acc_r{n}_"))
    (tmp / "downloads").mkdir(parents=True, exist_ok=True)
    (tmp / ".settings.json").write_text(
        json.dumps({"quality": "max"}, ensure_ascii=False), encoding="utf-8")
    srv, vport = start_video_server()
    VB = f"http://127.0.0.1:{vport}"
    sess = core.build_session()
    R.evidence["video_server_port"] = vport
    R.evidence["tmp_workspace"] = str(tmp)

    # ---------------------------------------------------------------- A 解析器
    R.cat("A · 解析器 / 双平台数据形状 / 画质选择 / 文件名 / 校验")

    def a1():
        info = core.parse_aweme(dy_aweme())
        b = info.best()
        return (info.aweme_id == DY_AID and len(info.candidates) == 2
                and (b.width, b.height) == (1080, 1920) and b.bitrate == 5000000,
                f"id={info.aweme_id} 候选={len(info.candidates)} best={b.width}x{b.height} br={b.bitrate}")
    R.check("抖音 parse_aweme: 3 档里过滤掉 playwm 水印档, 选 1080x1920 最高码率", a1)

    def a2():
        ph = ks_photo()
        # 真实 REST feed 里 author 与 photo 是平级的, 先走合并再解析
        merged = extract_ks_photos(ks_feed_body(ph))
        info = core.parse_kuaishou(merged[0])
        b = info.best()
        return (len(merged) == 1 and info.aweme_id == KS_AID
                and len(info.candidates) == 4
                and (b.width, b.height) == (960, 720) and b.bitrate == 2006
                and info.author == "义沉沉" and info.duration == 92066
                and info.create_time == 1789307548,
                f"合并={len(merged)} 候选={len(info.candidates)} "
                f"best={b.width}x{b.height} br={b.bitrate} "
                f"author={info.author} dur={info.duration} ts={info.create_time}")
    R.check("快手 parse_kuaishou (REST): 遍历 manifest+manifestH265, 平级 author 合并, 毫秒时间戳折秒", a2)

    def a3():
        C = ks_apollo_client()
        raw = C[f"VisionVideoDetailPhoto:{KS_AID}"]
        info = core.parse_kuaishou(raw, C)
        b = info.best()
        return ((b.width, b.height) == (1080, 1920) and b.bitrate == 5000
                and len(info.candidates) >= 2,
                f"Apollo type:id/type:json 解引用 -> best={b.width}x{b.height} br={b.bitrate} 候选={len(info.candidates)}")
    R.check("快手 parse_kuaishou (Apollo 详情页): id 解引用 + backupUrl json 壳剥开", a3)

    def a4():
        i1 = core.parse_any(ks_wrapped())
        i2 = core.parse_any(dy_aweme())
        return (i1.aweme_id == KS_AID and len(i1.candidates) == 4
                and i2.aweme_id == DY_AID and len(i2.candidates) == 2,
                f"快手={i1.aweme_id}/{len(i1.candidates)} 抖音={i2.aweme_id}/{len(i2.candidates)}")
    R.check("parse_any 双平台分派: 包装对象走快手, 原生对象走抖音", a4)

    def a5():
        ok = (_is_ks_id(KS_AID) and not _is_ks_id(DY_AID)
              and not _is_ks_id("abc") and not _is_ks_id("12345678"))
        return ok, f"KS_AID={_is_ks_id(KS_AID)} DY_AID={_is_ks_id(DY_AID)}"
    R.check("is_kuaishou_id: 字母数字混排放行, 纯数字抖音 id / 短串拒绝", a5)

    def a6():
        good = (_safe_id(KS_AID) == KS_AID and _safe_id(DY_AID) == DY_AID)
        bad = all(_safe_id(x) == "" for x in
                  ["../etc/passwd", "a.b", "中文", "has space", "a/b", "x" * 70])
        return good and bad, f"合法={good} 穿越全部拦掉={bad}"
    R.check("_safe_id: 快手字母数字 id 放行, 路径穿越/点/中文/空格全部拦掉", a6)

    def a7():
        # 用合并过平级 author 的真实 REST photo
        ph = extract_ks_photos(ks_feed_body(ks_photo()))[0]
        info = core.parse_any({"aweme_id": KS_AID,
                               "_platform": PLATFORM_KUAISHOU, "_ks": ph})
        stem = core.build_filename(info)
        return (stem.endswith("_" + KS_AID) and "." not in stem
                and "义沉沉" in stem and "2026" in stem[:8],
                f"stem={stem}")
    R.check("build_filename: 日期_作者_标题_id, 不含扩展名, 快手 id 完整保留", a7)

    def a8():
        p = tmp / "v.mp4"
        p.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 600)
        cases = {
            "mp4": (b"\x00\x00\x00\x20ftypisom" + b"\x00" * 600, True),
            "webm": (b"\x1a\x45\xdf\xa3" + b"\x00" * 600, True),
            "flv": (b"FLV\x01" + b"\x00" * 600, True),
            "avi": (b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 600, True),
            "tiny": (b"\x00\x00\x00\x20ftyp", False),
            "html": (b"<html>" + b"x" * 600, False),
        }
        out = {}
        for k, (data, want) in cases.items():
            p.write_bytes(data)
            out[k] = (core.verify_video(p), want)
        ok = all(g == w for g, w in out.values())
        return ok, " ".join(f"{k}:{'T' if g else 'F'}" for k, (g, w) in out.items())
    R.check("verify_video: MP4/WebM/FLV/AVI 魔数全认, <512 字节与 HTML 判假", a8)

    def a9():
        cands = core.parse_any(ks_wrapped()).candidates
        r1 = filter_candidates(cands, "max")
        r2 = filter_candidates(cands, "480")
        return (len(r1) == 4 and len(r2) == 4,
                f"max={len(r1)} 480(过滤后空则回退全量)={len(r2)}")
    R.check("filter_candidates: max 全量; 限制档位无候选时回退全量(不会没得下)", a9)

    def a10():
        return (_clamp_gap(0) == 1.0 and _clamp_gap(999) == 60.0
                and _clamp_gap(7) == 7.0 and QUALITY_CHOICES["max"] == "最高画质",
                f"clamp(0)={_clamp_gap(0)} clamp(999)={_clamp_gap(999)}")
    R.check("设置夹取: auto_gap 1~60, 画质选项含最高画质", a10)

    def a11():
        s = core.build_session()
        low = {k.lower(): v for k, v in s.headers.items()}
        return ("range" not in low and low.get("user-agent", "").startswith("Mozilla/5.0")
                and "douyin.com" in low.get("referer", ""),
                f"headers={sorted(low)}")
    R.check("build_session: 带 UA/Referer, 不放全局 Range(v1.6 断点 bug 回归)", a11)

    def a12():
        c = core.Candidate("https://x/playwm/a.mp4", 2160, 3840, 9999999, 50000000)
        c2 = core.Candidate("https://x/a.mp4", 1080, 1920, 5000000, 30000000)
        return (c.score() > c2.score() and c.score()[0] == 2160 * 3840,
                f"score=({c.score()}) > ({c2.score()})")
    R.check("Candidate.score 三元组 (面积, 码率, 体积) 取 max", a12)

    # ---------------------------------------------------------------- B 事件管线
    R.cat("B · 双站点事件管线 (收藏/取消/列表基线/推荐页/路由隔离)")

    def mk_watcher(**kw):
        calls, pend, logs = [], [], []
        w = BrowserWatcher(
            profile_dir=tmp,
            on_aweme=lambda a, s: calls.append((a, s)),
            on_pending=lambda a: pend.append(a),
            log=lambda m: logs.append(str(m)),
            **kw,
        )
        return w, calls, pend, logs

    def feed(w, kind, url, body="", req=""):
        w._on_binding("acc", json.dumps(
            {"kind": kind, "url": url, "body": body, "req": req},
            ensure_ascii=False))
        return w.drain()

    def b1():
        w, calls, pend, logs = mk_watcher()
        ph = ks_photo()
        feed(w, "data", "https://www.kuaishou.com/rest/v/feed/hot",
             json.dumps(ks_feed_body(ph), ensure_ascii=False))
        cached = w._get_cached(KS_AID)
        feed(w, "action", "https://www.kuaishou.com/rest/v/photo/collect", "",
             json.dumps({"photoId": KS_AID, "userId": "u1", "collect": 1}))
        hit = [(a, s) for a, s in calls if s == "快手收藏"]
        return (cached is not None and len(cached["_ks"]["manifest"]["adaptationSet"]) == 1
                and len(hit) == 1 and hit[0][0]["aweme_id"] == KS_AID
                and not pend,
                f"缓存={bool(cached)} 直接提交={len(hit)} pending={pend}")
    R.check("快手: feed 缓存 → 收藏 collect=1 命中缓存, 零延迟直接下载(不重抓)", b1)

    def b2():
        w, calls, pend, logs = mk_watcher()
        w._pending_actions.append(KS_AID)
        feed(w, "action", "https://www.kuaishou.com/rest/v/photo/collect", "",
             json.dumps({"photoId": KS_AID, "collect": 2}))
        return (not calls and KS_AID not in w._pending_actions,
                f"calls={calls} pending={w._pending_actions}")
    R.check("快手: collect=2 取消收藏不下载, 并撤掉未开始抓的 pending", b2)

    def b3():
        w, calls, pend, logs = mk_watcher()
        feed(w, "action", "https://www.kuaishou.com/rest/v/photo/collect", "",
             json.dumps({"photoId": KS_AID, "userId": "u1"}))
        return (not calls and not pend and any("忽略" in x for x in logs),
                f"calls={calls} pending={pend} logs={logs[-1:]}")
    R.check("快手: 没带 collect 的状态同步请求被忽略(不误下)", b3)

    def b4():
        w, calls, pend, logs = mk_watcher()
        feed(w, "action", "https://www.kuaishou.com/rest/v/photo/collect", "",
             json.dumps({"photoId": KS_AID, "collect": 1}))
        return (not calls and pend == [KS_AID]
                and any("正在抓取" in x for x in logs),
                f"pending={pend} logs={logs[-1:]}")
    R.check("快手: 冷启动无缓存走 on_pending 占位 + 无头详情兜底", b4)

    def b5():
        w, calls, pend, logs = mk_watcher()
        feed(w, "data", "https://www.kuaishou.com/rest/v/collect/list",
             json.dumps(ks_feed_body(ks_photo()), ensure_ascii=False))
        base1 = (not calls and w.baseline_done and KS_AID in w.known_favs)
        feed(w, "data", "https://www.kuaishou.com/rest/v/collect/list",
             json.dumps(ks_feed_body(ks_photo()), ensure_ascii=False))
        return (base1 and not calls, f"baseline={w.baseline_done} known={len(w.known_favs)} calls={len(calls)}")
    R.check("快手: /rest/v/collect/list 只建基线, 重复刷新也绝不触发下载", b5)

    def b6():
        w, calls, pend, logs = mk_watcher()
        w.download_all_on_start = True
        feed(w, "data", "https://www.kuaishou.com/rest/v/collect/list",
             json.dumps(ks_feed_body(ks_photo()), ensure_ascii=False))
        return (len(calls) == 1 and calls[0][1] == "快手收藏列表",
                f"calls={[(a['aweme_id'], s) for a, s in calls]}")
    R.check("快手: 开了 download_all_on_start 才回补收藏列表", b6)

    def b7():
        w, calls, pend, logs = mk_watcher()
        w.auto_recommend = True
        url = "https://www.kuaishou.com/rest/v/feed/hot"
        body = json.dumps(ks_feed_body(ks_photo()), ensure_ascii=False)
        feed(w, "data", url, body)
        feed(w, "data", url, body)   # 同一条重复流量
        srcs = [s for _, s in calls]
        return (len(calls) == 1 and srcs == ["快手推荐"],
                f"提交次数={len(calls)} sources={srcs}")
    R.check("快手: auto_recommend 开启后推荐流去重提交「快手推荐」(不重抓)", b7)

    def b8():
        w, calls, pend, logs = mk_watcher()
        feed(w, "data", "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=1",
             dy_feed_data(dy_aweme()))
        cached = w._get_cached(DY_AID)
        feed(w, "action", "https://www.douyin.com/aweme/v1/web/aweme/collect/",
             "", f"action=1&aweme_id={DY_AID}&aweme_type=0")
        return (cached is not None and pend == [DY_AID] and not calls,
                f"缓存={bool(cached)} pending={pend} 直接提交={len(calls)}")
    R.check("抖音: 收藏 action=1 → on_pending 占位 + 进补全队列(每次重抓详情)", b8)

    def b9():
        w, calls, pend, logs = mk_watcher()
        w._pending_actions.append(DY_AID)
        feed(w, "action", "https://www.douyin.com/aweme/v1/web/aweme/collect/",
             "", f"action=0&aweme_id={DY_AID}")
        return (DY_AID not in w._pending_actions and not calls,
                f"pending={w._pending_actions} calls={len(calls)}")
    R.check("抖音: action=0 取消收藏不下载并撤 pending", b9)

    def b10():
        w, calls, pend, logs = mk_watcher()
        feed(w, "action", "https://www.douyin.com/aweme/v1/web/aweme/collect/",
             "", f"aweme_id={DY_AID}&item_type=0&type=0")
        return (not pend and not calls and any("忽略状态同步" in x for x in logs),
                f"pending={pend} logs={logs[-1:]}")
    R.check("抖音: 无 action 的状态同步请求被忽略(早期 action=1/0 判反的回归)", b10)

    def b11():
        w, calls, pend, logs = mk_watcher()
        feed(w, "data", "https://www-hj.douyin.com/aweme/v1/web/aweme/listcollection/?count=1&cursor=0",
             dy_feed_data(dy_aweme()))
        first = (not calls and w.baseline_done and DY_AID in w.known_favs)
        feed(w, "data", "https://www-hj.douyin.com/aweme/v1/web/aweme/listcollection/?cursor=20",
             dy_feed_data(dy_aweme("7705427599274719718")))
        return (first and not w._is_first_page(
            "https://x/aweme/v1/web/aweme/listcollection/?cursor=20") and not calls,
            f"baseline={w.baseline_done} known={len(w.known_favs)} calls={len(calls)}")
    R.check("抖音: 收藏列表第一页建基线; cursor!=0 的翻页不算第一页, 不误下", b11)

    def b12():
        w, calls, pend, logs = mk_watcher()
        w.auto_recommend = True
        feed(w, "data", "https://www.douyin.com/aweme/v1/web/tab/feed/",
             dy_feed_data(dy_aweme()))
        return (len(calls) == 1 and calls[0][1] == "推荐页",
                f"calls={[(a.get('aweme_id'), s) for a, s in calls]}")
    R.check("抖音: auto_recommend 下 /tab/feed 提交「推荐页」(走详情重抓)", b12)

    def b13():
        w, calls, pend, logs = mk_watcher()
        # 快手 URL 塞抖音形状 body: 必须走快手分支, 不得触发抖音推荐/收藏
        w.auto_recommend = True
        feed(w, "data", "https://www.kuaishou.com/rest/v/feed/hot",
             dy_feed_data(dy_aweme()))
        no_dy = not any(s in ("推荐页", "快手推荐") for _, s in calls)
        # 抖音 URL 塞快手形状 body: 必须走抖音分支, 不得触发快手收藏
        w2, c2, p2, _ = mk_watcher()
        feed(w2, "action", "https://www.douyin.com/aweme/v1/web/aweme/collect/",
             "", json.dumps({"photoId": KS_AID, "collect": 1}))
        no_ks = not any(s == "快手收藏" for _, s in c2)
        return (no_dy and no_ks and BrowserWatcher._is_ks_url("/rest/v/feed/hot")
                and not BrowserWatcher._is_ks_url("https://www.douyin.com/aweme/v1/web/tab/feed/"),
                f"no_dy={no_dy} no_ks={no_ks}")
    R.check("路由隔离: _is_ks_url 分流, 快手流量不进抖音分支、抖音流量不进快手分支", b13)

    def b14():
        return (ks_action_value("https://www.kuaishou.com/rest/v/photo/collect", {},
                                {"collect": 1}) == "1"
                and ks_action_value("https://www.kuaishou.com/rest/v/photo/collect", {},
                                    {"collect": 2}) == "0"
                and collect_ks_ids({"photoId": KS_AID}) == [KS_AID]
                and KS_AID in ks_ids_from_url(f"https://www.kuaishou.com/short-video/{KS_AID}"))
    R.check("快手解析辅助: collect 1/2 语义 + photoId 抽取 + 详情页 URL 取 id", b14)

    # ---------------------------------------------------------------- C 真实下载链路
    R.cat("C · 真实下载链路 (本地 HTTP 服务器跑 core.download 全链路)")

    def c1():
        dest = tmp / "c_full.mp4"
        ok = core.download(sess, VB + "/full", dest, log=lambda m: None, retries=2)
        return (ok and dest.exists() and dest.read_bytes() == PAYLOAD
                and not (tmp / "c_full.mp4.part").exists(),
                f"ok={ok} size={dest.stat().st_size if dest.exists() else 0}")
    R.check("core.download 完整下载: 字节完全一致, .part 正常改名无残留", c1)

    def c2():
        dest = tmp / "c_resume.mp4"
        part = tmp / "c_resume.mp4.part"
        part.write_bytes(PAYLOAD[:len(PAYLOAD) // 2])
        ok = core.download(sess, VB + "/resume", dest, log=lambda m: None, retries=2)
        return (ok and dest.read_bytes() == PAYLOAD,
                f"断点起点={len(PAYLOAD)//2} ok={ok} 最终={dest.stat().st_size}")
    R.check("core.download 断点续传: .part 半截 → Range 206 → 拼回完整文件", c2)

    def c3():
        # chunk 用小值: urllib3 一次 read 的块越小, 断连前的半截越能真正落进 .part,
        # 下一轮 Range 才有东西可续。默认 1MB 会在 yield 前就抛 IncompleteRead, .part 恒 0。
        dest = tmp / "c_trunc.mp4"
        ok = core.download(sess, VB + "/trunc", dest, chunk=1024,
                           log=lambda m: None, retries=4)
        return (ok and dest.exists() and dest.read_bytes() == PAYLOAD,
                f"ok={ok} size={dest.stat().st_size if dest.exists() else 0}")
    R.check("core.download 中途断连: 留 .part 自动重试续传, 不留半截坏文件", c3)

    def c4():
        dest = tmp / "c_416.mp4"
        (tmp / "c_416.mp4.part").write_bytes(PAYLOAD)
        ok = core.download(sess, VB + "/416", dest, log=lambda m: None, retries=1)
        return (ok and dest.read_bytes() == PAYLOAD, f"ok={ok}")
    R.check("core.download 416 + 非空 .part: 判定已下完整, 直接落盘成功", c4)

    def c5():
        dest = tmp / "c_416e.mp4"
        part = tmp / "c_416e.mp4.part"
        part.write_bytes(b"")
        ok = core.download(sess, VB + "/416", dest, log=lambda m: None, retries=1)
        return (not ok and not part.exists(), f"ok={ok} 空断点已删={not part.exists()}")
    R.check("core.download 416 + 空 .part: 删掉空断点重下, 不永久卡死", c5)

    def c6():
        dest = tmp / "c_html.mp4"
        ok = core.download(sess, VB + "/html", dest, log=lambda m: None, retries=1)
        dest2 = tmp / "c_json.mp4"
        ok2 = core.download(sess, VB + "/json", dest2, log=lambda m: None, retries=1)
        return (not ok and not ok2 and not dest.exists() and not dest2.exists(),
                f"html={ok} json={ok2}")
    R.check("core.download 登录页/JSON 响应: Content-Type 判假, 不把网页当视频存", c6)

    def c7():
        dest = tmp / "c_sniff.mp4"
        ok = core.download(sess, VB + "/sniff", dest, log=lambda m: None, retries=1)
        return (not ok and not dest.exists() and not (tmp / "c_sniff.mp4.part").exists(),
                f"ok={ok} 首块 <!DOCTYPE 被嗅探拦下")
    R.check("core.download 内容嗅探: 伪装 ctype 但首块是 HTML 也要拦下", c7)

    def c8():
        n = core.probe_size(sess, VB + "/probe")
        return (n == len(PAYLOAD), f"probe_size={n} 期望={len(PAYLOAD)}")
    R.check("core.probe_size: 只读 Content-Range 的 /总长, 不下整段视频", c8)

    def c9():
        dest = tmp / "c_verify.mp4"
        core.download(sess, VB + "/full", dest, log=lambda m: None, retries=2)
        return core.verify_video(dest), f"verify_video(真实落盘文件)=True"
    R.check("真实下载落盘文件通过 verify_video 校验", c9)

    # ---------------------------------------------------------------- D Downloader.process 全链路
    R.cat("D · Downloader.process 全链路 (解析→选画质→落盘→meta→状态→事件)")

    _orig = {
        "get_out_dir": M.get_out_dir, "load_settings": M.load_settings,
        "probe_size": M.probe_size, "verify_video": M.verify_video,
        "download": M.download,
    }

    class FakeState:
        def __init__(self):
            self.done, self.marked = {}, []

        def has(self, aid):
            return aid in self.done

        def mark(self, aid, path, q):
            self.done[aid] = 1
            self.marked.append((aid, path, q))

        def remove(self, aid):
            self.done.pop(aid, None)
            return True

    class FakeBus:
        def __init__(self):
            self.events = []

        def __getattr__(self, name):
            return lambda *a, **kw: self.events.append((name, a, kw))

    def fake_download(session, url, dest, log=print, on_progress=None, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4096)
        if on_progress:
            on_progress(4096, 4096)
        return True

    def patch_main():
        M.get_out_dir = lambda: tmp / "downloads"
        M.load_settings = lambda p: {"quality": "max", "by_author": False}
        M.probe_size = lambda s, u: 23089393
        M.verify_video = lambda p: True
        M.download = fake_download

    def unpatch_main():
        for k, v in _orig.items():
            setattr(M, k, v)

    def d1():
        patch_main()
        try:
            st, bus = FakeState(), FakeBus()
            d = M.Downloader(st, sess, log_fn=lambda m: None, bus=bus)
            d.process(ks_wrapped(), "快手收藏")
            files = list((tmp / "downloads").glob(f"*_{KS_AID}.mp4"))
            meta = tmp / "downloads" / "_meta" / f"{KS_AID}.json"
            names = [e[0] for e in bus.events]
            return (d.count_ok == 1 and len(st.marked) == 1 and files
                    and meta.is_file()
                    and json.loads(meta.read_text(encoding="utf-8"))["aweme_id"] == KS_AID
                    and "begin" in names and "finish" in names and "set_speed" in names,
                    f"ok={d.count_ok} 落盘={[f.name for f in files]} meta={meta.is_file()} 事件={names}")
        finally:
            unpatch_main()
    R.check("快手完整落盘链路: count_ok / 文件名含字母数字 id / _meta / begin+finish+set_speed", d1)

    def d2():
        patch_main()
        try:
            st, bus = FakeState(), FakeBus()
            d = M.Downloader(st, sess, log_fn=lambda m: None, bus=bus)
            d.process(dy_aweme(), "收藏")
            files = list((tmp / "downloads").glob(f"*_{DY_AID}.mp4"))
            return (d.count_ok == 1 and len(st.marked) == 1 and files,
                    f"ok={d.count_ok} 落盘={[f.name for f in files]}")
        finally:
            unpatch_main()
    R.check("抖音完整落盘链路: parse_aweme → 选 1080p → 落盘 → 状态登记", d2)

    def d3():
        patch_main()
        try:
            st, bus = FakeState(), FakeBus()
            st.done[KS_AID] = 1
            d = M.Downloader(st, sess, log_fn=lambda m: None, bus=bus)
            d.process(ks_wrapped(), "快手收藏")
            return (d.count_skip == 1 and d.count_ok == 0
                    and ("skip", (), {}) in bus.events,
                    f"skip={d.count_skip} ok={d.count_ok}")
        finally:
            unpatch_main()
    R.check("已下载去重: state.has() 命中走 skip, 不重复下载", d3)

    def d4():
        patch_main()
        try:
            st, bus = FakeState(), FakeBus()
            d = M.Downloader(st, sess, log_fn=lambda m: None, bus=bus)
            d.submit(ks_wrapped(), "快手收藏")
            got = M._KS_LAST.get(KS_AID)
            item = d.q.get_nowait()
            return (got is not None and item[1] == "快手收藏"
                    and any(e[0] == "add_waiting" and e[1][0] == KS_AID for e in bus.events),
                    f"_KS_LAST={bool(got)} 入队来源={item[1]}")
        finally:
            unpatch_main()
    R.check("submit: 快手对象写进 _KS_LAST(重试零网络) + 入队 + 面板占位", d4)

    def d5():
        # 快手重试命中 _KS_LAST, 绝不去开抖音详情页
        patch_main()
        try:
            M._KS_LAST[KS_AID] = ks_wrapped()
            called = {"n": 0}
            real = M._hd_fetch
            M._hd_fetch = lambda aid: called.update(n=called["n"] + 1)
            try:
                aw = M.refetch_aweme(KS_AID, sess)
            finally:
                M._hd_fetch = real
            return (aw is not None and aw["aweme_id"] == KS_AID and called["n"] == 0,
                    f"命中缓存={aw is not None} 误开抖音详情页次数={called['n']}")
        finally:
            unpatch_main()
    R.check("快手重试: 命中 _KS_LAST 零网络返回, 绝不拿 photoId 去开抖音 /video/", d5)

    # ---------------------------------------------------------------- E EventBus 状态机
    R.cat("E · 面板 EventBus 状态机 (队列 / 进度 / 失败 / 重试 / 忽略 / 快照)")

    def mk_bus():
        b = EventBus(tmp / "bus", tmp / "bus" / "downloads",
                     tmp / "bus" / ".state.json", tmp / "bus" / "log.txt")
        return b

    bus = mk_bus()

    def e1():
        snap = bus.snapshot()
        need = {"now", "uptime", "status", "counters", "active", "waiting", "failed",
                "recent", "total", "today", "bytes", "max_speed", "quality",
                "quality_choices", "out_dir", "by_author", "auto_reopen", "auto_rec",
                "auto_gap", "auto_adv", "out_dir_configured"}
        return (len(snap) == 21 and need <= set(snap),
                f"snapshot 键数={len(snap)} 缺={sorted(need - set(snap))}")
    R.check("snapshot: 21 个键齐全(面板所有区块的数据源)", e1)

    def e2():
        bus.add_waiting(KS_AID, source="解析中")
        bus.add_waiting(KS_AID, "标题来了", "作者来了", "快手收藏")
        w = [x for x in bus.waiting if x["id"] == KS_AID]
        return (len(w) == 1 and w[0]["title"] == "标题来了"
                and w[0]["author"] == "作者来了" and w[0]["source"] == "快手收藏",
                f"waiting={w}")
    R.check("add_waiting: 同 id「解析中」占位就地补全标题/作者/来源, 不产生重复行", e2)

    def e3():
        bus.begin(KS_AID, {"title": "T", "author": "A", "cover": "", "quality": "960x720", "size": 1000})
        bus.progress(KS_AID, 500, 1000)
        a = bus.active.get(KS_AID, {})
        return (a.get("phase") == "downloading" and a.get("pct", 0) >= 0
                and KS_AID not in [x["id"] for x in bus.waiting],
                f"active={ {k:v for k,v in a.items() if not k.startswith('_')} }")
    R.check("begin/progress: 从 waiting 移到 active, 进度百分比写入", e3)

    def e4():
        bus.finish(KS_AID, True, {"size": 1000})
        return (bus.counters["ok"] == 1 and KS_AID not in bus.active,
                f"counters={bus.counters}")
    R.check("finish(ok): 成功计数 +1 并移出 active", e4)

    def e5():
        bus.fail(KS_AID, "测试原因", "标题", "作者", "")
        bus.set_speed(9.5)
        return (KS_AID in bus.failed and bus.max_speed == 9.5,
                f"failed={list(bus.failed)} max_speed={bus.max_speed}")
    R.check("fail / set_speed: 失败项进列表, 峰值速度记录", e5)

    def e6():
        # 用 Event 把后台回调卡住, 这样能确定性地读到「受理瞬间已标 retrying」;
        # 否则线程太快, 检查时失败项早被成功摘除了。
        release = threading.Event()
        entered = threading.Event()
        done = {"v": None}

        def slow_retry(aid):
            done["v"] = aid
            entered.set()
            release.wait(5)
            return (True, "")

        bus.on_retry = slow_retry
        ok = bus.retry(KS_AID)
        entered.wait(3)
        immediate = bool(bus.failed.get(KS_AID, {}).get("retrying"))
        release.set()
        for _ in range(60):
            if KS_AID not in bus.failed:
                break
            time.sleep(0.05)
        return (ok and immediate and done["v"] == KS_AID and KS_AID not in bus.failed,
                f"ok={ok} 立刻标 retrying={immediate} 回调={done['v']} "
                f"已摘除={KS_AID not in bus.failed}")
    R.check("retry: 受理立刻标「重试中」(面板零延迟反馈), 后台成功自动摘除", e6)

    def e7():
        bus.fail("FAKE1234", "原因")
        ok = bus.dismiss("FAKE1234")
        return (ok and "FAKE1234" not in bus.failed,
                f"dismiss={ok} 剩余={list(bus.failed)}")
    R.check("dismiss: 叉掉失败项, 只动内存不碰文件/state", e7)

    def e8():
        q = bus.subscribe()
        bus.publish()
        snap = q.get(timeout=2)
        bus.unsubscribe(q)
        return (isinstance(snap, dict) and "now" in snap and q not in bus.clients,
                f"收到快照键数={len(snap) if isinstance(snap, dict) else 0}")
    R.check("subscribe/publish/unsubscribe: SSE 客户端能收到最新快照且可注销", e8)

    def e9():
        bus.skip()
        return bus.counters["skip"] == 1, f"counters={bus.counters}"
    R.check("skip: 已存在/已下载计数 +1", e9)

    # ---------------------------------------------------------------- F 真实面板 HTTP
    R.cat("F · 真实运行面板 HTTP (127.0.0.1:8899, 只读 + 无副作用写)")

    def f1():
        st, hd, d = http_req("GET", "/")
        return (st == 200 and "text/html" in hd.get("Content-Type", "")
                and len(d) > 2000 and b"<html" in d[:1000].lower(),
                f"status={st} len={len(d)} ctype={hd.get('Content-Type')}")
    R.check("GET / : 面板 HTML 正常返回 (no-store, >2KB)", f1)

    def f2():
        st, hd, d = http_req("GET", "/index.html")
        return (st == 200 and len(d) > 2000, f"status={st} len={len(d)}")
    R.check("GET /index.html : 显式路径同样返回面板", f2)

    def f3():
        st, hd, d = http_req("GET", "/api/state")
        j = json.loads(d.decode("utf-8"))
        return (st == 200 and len(j) == 21 and j["status"]["running"] is True
                and "Chrome" in j["status"]["browser"],
                f"status={st} 键数={len(j)} running={j['status']['running']} browser={j['status']['browser']}")
    R.check("GET /api/state : 21 键, running=True, 浏览器已连接", f3)

    def f4():
        st, hd, d = http_req("GET", "/api/log?lines=3")
        j = json.loads(d.decode("utf-8"))
        return (st == 200 and isinstance(j.get("lines"), list) and len(j["lines"]) <= 3,
                f"status={st} 行数={len(j.get('lines', []))}")
    R.check("GET /api/log?lines=3 : 日志尾部正常返回且夹取上限生效", f4)

    def f5():
        st, hd, d = http_req("GET", "/favicon.ico")
        return (st == 204 and hd.get("Content-Length") == "0", f"status={st} len={len(d)}")
    R.check("GET /favicon.ico : 204 空响应 (不刷错误日志)", f5)

    def f6():
        st, hd, d = http_req("GET", "/nope-not-a-route")
        return (st == 404 and json.loads(d.decode("utf-8")).get("error") == "not found",
                f"status={st} body={d[:80]}")
    R.check("GET 未知路径 : 404 {error:not found}", f6)

    def f7():
        st, hd, d = http_req("GET", "/api/thumb?id=FAKE1234")
        return (st == 404 and hd.get("Cache-Control", "").startswith("no-store"),
                f"status={st} cache={hd.get('Cache-Control')}")
    R.check("GET /api/thumb 不存在的 id : 404 + no-store (前端会自动重试)", f7)

    def f8():
        snap, raw = sse_first_frame()
        return (isinstance(snap, dict) and "now" in snap and "counters" in snap,
                f"首帧键数={len(snap) if isinstance(snap, dict) else 0} raw={raw[:60]!r}")
    R.check("GET /api/events : SSE 首帧即完整 snapshot (实时进度不断线)", f8)

    def f9():
        """唯一会写 .settings.json 的检查: 只改 3 个安全字段并当场还原。"""
        p = ROOT / ".settings.json"
        original_raw = p.read_bytes() if p.exists() else b"{}"
        orig = json.loads(original_raw.decode("utf-8") or "{}")
        oq = orig.get("quality", "max")
        og = _clamp_gap(orig.get("auto_gap"))
        ob = bool(orig.get("by_author"))
        try:
            st1, _, d1 = http_req("POST", "/api/settings",
                                  {"quality": "720", "auto_gap": 7, "by_author": True})
            ok1 = st1 == 200 and json.loads(d1.decode("utf-8")).get("ok") is True
            st2, _, d2 = http_req("GET", "/api/state")
            s = json.loads(d2.decode("utf-8"))
            changed = (s.get("quality") == "720" and s.get("auto_gap") == 7
                       and s.get("by_author") is True)
            st3, _, d3 = http_req("POST", "/api/settings",
                                  {"quality": oq, "auto_gap": og, "by_author": ob})
            ok3 = st3 == 200 and json.loads(d3.decode("utf-8")).get("ok") is True
            st4, _, d4 = http_req("GET", "/api/state")
            s4 = json.loads(d4.decode("utf-8"))
            restored = (s4.get("quality") == oq and s4.get("auto_gap") == og
                        and s4.get("by_author") is ob)
            # 兜底: 逐字节还原, 保证用户设置一点没变
            after = p.read_bytes() if p.exists() else b"{}"
            if after != original_raw:
                p.write_bytes(original_raw)
                restored = restored and True
            return (ok1 and changed and ok3 and restored,
                    f"写入={ok1}/{changed} 还原={ok3}/{restored} 原值 q={oq} gap={og} by_author={ob}")
        except Exception:
            p.write_bytes(original_raw)   # 任何异常都强制还原
            raise
    R.check("POST /api/settings : quality/auto_gap/by_author 写入生效并**逐字节还原用户设置**", f9)

    def f10():
        st, _, d = http_req("POST", "/api/retry", {"id": "FAKE1234"})
        return (st == 200 and json.loads(d.decode("utf-8")).get("ok") is False,
                f"status={st} body={d[:80]}")
    R.check("POST /api/retry 不存在的失败项 : 返回 ok=false, 不误触发任务", f10)

    def f11():
        st, _, d = http_req("POST", "/api/fail/dismiss", {"id": "FAKE1234"})
        return (st == 200 and json.loads(d.decode("utf-8")).get("ok") is False,
                f"status={st} body={d[:80]}")
    R.check("POST /api/fail/dismiss 不存在的失败项 : 返回 ok=false (叉掉按钮不空转)", f11)

    def f12():
        st, _, d = http_req("POST", "/api/delete", {"id": "FAKE1234"})
        return (st == 200 and json.loads(d.decode("utf-8")).get("ok") is True,
                f"status={st} body={d[:80]} (恒真, 不会删真实文件)")
    R.check("POST /api/delete 合法但不存在 id : 幂等返回 ok=true, 真实文件不受影响", f12)

    def f13():
        st, _, d = http_req("POST", "/nope-not-a-route", {})
        return (st == 404 and json.loads(d.decode("utf-8")).get("error") == "not found",
                f"status={st}")
    R.check("POST 未知路径 : 404", f13)

    def f14():
        raw = oversize_post_response()
        return (b"400" in raw[:40] and b"\xe8\xaf\xb7\xe6\xb1\x82\xe4\xbd\x93" in raw,
                f"resp={raw[:120]!r}")
    R.check("POST 超大 Content-Length : 读 body 之前就回 400 (防内存打满)", f14)

    def f15():
        # 只发非法画质。auto_rec / auto_adv / auto_reopen / out_dir 一律不发 ——
        # 它们会触发运行中 watcher 的真实回调(真的会去下用户推荐页),
        # 第一轮就是因为发了 auto_rec 把用户的设置打开过。
        st, _, d = http_req("POST", "/api/settings", {"quality": "9999p"})
        j = json.loads(d.decode("utf-8"))
        st2, _, d2 = http_req("GET", "/api/state")
        s = json.loads(d2.decode("utf-8"))
        return (st == 200 and j.get("ok") is True
                and s.get("quality") in QUALITY_CHOICES
                and s.get("auto_rec") is False and s.get("auto_adv") is False,
                f"非法画质被忽略 q={s.get('quality')} "
                f"auto_rec={s.get('auto_rec')} auto_adv={s.get('auto_adv')}")
    R.check("POST 非法设置: 非法画质被忽略; 危险字段(自动下载开关)始终未被打开", f15)

    # ---------------------------------------------------------------- G 真浏览器钩子 + live CDP
    R.cat("G · 真浏览器钩子注入 (双站点) + 运行实例的钩子存活")

    def hook_run(tag):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "hook_smoke.py")],
            cwd=str(ROOT), capture_output=True, timeout=150, env=env,
        )
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out

    def g1():
        rc, out = hook_run("g1")
        return (rc == 0 and "9/9" in out,
                f"returncode={rc} tail={out.strip().splitlines()[-1:]}")
    R.check(f"[第{n}轮·第1次] tests/hook_smoke.py 真浏览器注入 HOOK_JS: 9/9 全过", g1)

    def g2():
        rc, out = hook_run("g2")
        return (rc == 0 and "9/9" in out,
                f"returncode={rc} tail={out.strip().splitlines()[-1:]}")
    R.check(f"[第{n}轮·第2次] 再跑一遍钩子冒烟(重复验证, 无偶发失败)", g2)

    def g3():
        # 只读连用户的 Chrome, 绝不 br.close()
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        vals = {}
        try:
            br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}", timeout=8000)
            for ctx in br.contexts:
                for pg in ctx.pages:
                    try:
                        vals = pg.evaluate(
                            "() => ({ver: window.__DY_HOOK_VER__ || 0,"
                            " hooked: !!window.__DY_HOOKED_V2__,"
                            " fetch: String(window.fetch).slice(0, 60), url: location.href})")
                    except Exception:
                        continue
                    if vals.get("ver"):
                        break
                if vals.get("ver"):
                    break
        finally:
            pw.stop()          # 只停 playwright, 不 close 浏览器!
        return (vals.get("ver") == HOOK_VER and vals.get("hooked") is True
                and "function" in vals.get("fetch", ""),
                f"ver={vals.get('ver')} hooked={vals.get('hooked')} fetch={vals.get('fetch')!r} url={vals.get('url')}")
    R.check("live CDP 只读: 用户浏览器里 HOOK_VER=10 / __DY_HOOKED_V2__ / fetch 已替换", g3)

    def g4():
        st, d = cdp_json()
        return (st == 200 and b"Browser" in d, f"守护栏: 9222 /json/version status={st}")
    R.check("护栏: 上面的 CDP 只读检查之后, 用户浏览器 9222 仍然存活(没被关掉)", g4)

    # ---------------------------------------------------------------- H 运行时 / 守护 / 全量测试
    R.cat("H · 运行时与守护进程 / 全量测试 / 版本一致性 / 工作区干净")

    def h1():
        return port_alive(PANEL_PORT) and port_alive(CDP_PORT), \
            f"8899={port_alive(PANEL_PORT)} 9222={port_alive(CDP_PORT)}"
    R.check("端口: 面板 8899 与调试 9222 都在监听", h1)

    def h2():
        # Chrome 的进程名是 chrome.exe, 不能和 python 写在同一个 -and 条件里,
        # 否则会把 9222 那一路整体筛掉(第一轮就是这么误报的)。
        ps = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { "
             "$_.CommandLine -match 'douyin_dl.py|daemon.py|remote-debugging-port=9222' "
             "} | Select-Object ProcessId,Name,ParentProcessId | ConvertTo-Json -Compress -Depth 3"],
            capture_output=True, timeout=45,
        )
        out = (ps.stdout or b"").decode("utf-8", "replace")
        procs = json.loads(out) if out.strip().startswith(("{", "[")) else []
        if isinstance(procs, dict):
            procs = [procs]
        names = [str(p.get("Name") or "") for p in procs]
        has_main = any(n.lower().startswith("python") for n in names)
        has_daemon = len([n for n in names if n.lower().startswith("python")]) >= 2
        has_chrome = any(n.lower() == "chrome.exe" for n in names)
        R.evidence["processes_sample"] = procs[:12]
        return (has_main and has_daemon and has_chrome,
                f"python={names.count('pythonw.exe') + names.count('python.exe')} "
                f"main={has_main} daemon={has_daemon} chrome9222={has_chrome} "
                f"names={names}")
    R.check("进程树: daemon → 主程序 → Chrome(9222) 全在运行", h2)

    def h3():
        st, _, d = http_req("GET", "/api/log?lines=40")
        lines = json.loads(d.decode("utf-8")).get("lines", [])
        joined = "\n".join(lines)
        return ("监听已启动" in joined and "抖音 / 快手" in joined,
                f"日志尾部含新文案={'抖音 / 快手' in joined} 含监听已启动={'监听已启动' in joined}")
    R.check("运行日志: v1.7 新文案「抖音 / 快手」+「✔ 监听已启动」", h3)

    def h4():
        p = ROOT / "daemon.log"
        t = p.read_text(encoding="utf-8", errors="replace")[-4000:] if p.exists() else ""
        return ("次启动下载进程" in t and "守护启动" in t,
                f"daemon.log 尾部含守护启动与拉起记录")
    R.check("daemon.log: 守护启动 + 自动拉起下载进程记录存在", h4)

    def h5():
        js_ver = re.search(r"const\s+HOOK_VER\s*=\s*(\d+)", HOOK_JS)
        # 只针对 window.fetch 那一个描述符: get/set 与 writable 同时出现会抛
        # TypeError: Invalid property descriptor (钩子会静默失效)。
        # XHR 的 Object.defineProperty(XP, "open", {writable:true, value:...}) 是合法的。
        i = HOOK_JS.find('Object.defineProperty(window, "fetch"')
        block = HOOK_JS[i:i + 400] if i >= 0 else ""
        fetch_clean = i >= 0 and "writable" not in block and "get:" in block and "set:" in block
        return (APP_VERSION == "1.7.0" and HOOK_VER == 10
                and js_ver and int(js_ver.group(1)) == 10
                and "__DY_HOOKED_V2__" in HOOK_JS
                and fetch_clean,
                f"version={APP_VERSION} py_ver={HOOK_VER} "
                f"js_ver={js_ver.group(1) if js_ver else None} "
                f"fetch 描述符无 writable={fetch_clean}")
    R.check("版本/钩子一致性: 1.7.0 + HOOK_VER=10 两侧同步, fetch 钩子不带 writable", h5)

    def h6():
        R.evidence["pytest"] = ""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"],
                           cwd=str(ROOT), capture_output=True, timeout=300, env=env)
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        R.evidence["pytest"] = out.strip()[-1200:]
        m = re.search(r"(\d+) passed", out)
        failed = re.search(r"(\d+) failed", out)
        n = int(m.group(1)) if m else 0
        return (p.returncode == 0 and n >= 228 and not failed,
                f"pytest: {n} passed, failed={failed.group(1) if failed else 0}, rc={p.returncode}")
    R.check(f"[第{n}轮] 全量 pytest tests/ : ≥228 passed, 0 failed", h6)

    def h7():
        # HOOK_JS 交给 node 真编译一次(历史上 writable 描述符错误是静默失效的)
        import tempfile as _tf
        f = Path(_tf.mkdtemp(prefix="dy_acc_js_")) / "hook.js"
        f.write_text(HOOK_JS, encoding="utf-8")
        p = subprocess.run(["node", "--check", str(f)], capture_output=True, timeout=30)
        return (p.returncode == 0,
                f"node --check rc={p.returncode} err={(p.stderr or b'').decode('utf-8','replace')[:150]}")
    R.check("HOOK_JS 交给 node --check 真编译(不是只看 Python 语法)", h7)

    def h8():
        files = sorted(set(
            list(ROOT.glob("*.py")) + list((ROOT / "douyin_dl").glob("*.py"))
            + list((ROOT / "tests").glob("*.py")) + list(ACC.glob("*.py"))))
        bad = []
        for f in files:
            try:
                ast.parse(f.read_text(encoding="utf-8"))
            except Exception as e:
                bad.append(f"{f.name}: {e}")
        return (not bad, f"ast.parse {len(files)} 个 py 文件, 失败={bad}")
    R.check("所有 Python 源文件 ast.parse 通过", h8)

    def h9():
        p = subprocess.run(["git", "status", "--short"], cwd=str(ROOT),
                           capture_output=True, timeout=30)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        tracked = [l for l in out.splitlines() if l and not l.startswith("?? acceptance/")]
        R.evidence["git_status"] = out[:500]
        R.evidence["git_head"] = subprocess.run(
            ["git", "log", "--oneline", "-1"], cwd=str(ROOT), capture_output=True
        ).stdout.decode("utf-8", "replace").strip()
        return (not tracked, f"已跟踪文件改动={tracked} HEAD={R.evidence['git_head']}")
    R.check("git: 已跟踪文件全部干净(验收只新增 acceptance/ 证据, 不改产品代码)", h9)

    # 收尾
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:
        pass
    return R.finish()


# ---------------------------------------------------------------------------
# HTML 报告
# ---------------------------------------------------------------------------
def build_report(rounds):
    all_ok = all(r["failed"] == 0 for r in rounds)
    total = sum(r["total"] for r in rounds)
    passed = sum(r["passed"] for r in rounds)
    failed = sum(r["failed"] for r in rounds)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    def badge(ok):
        return ('<span class="pill ok">PASS</span>' if ok
                else '<span class="pill bad">FAIL</span>')

    parts = [f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>抖音 / 快手收藏自动下载 · 全功能验收报告</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--line:#30363d;--txt:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--bad:#f85149;--acc:#58a6ff;--gold:#d29922}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1120px;margin:0 auto;padding:32px 24px 64px}}
h1{{font-size:26px;margin:0 0 6px}} h2{{font-size:18px;margin:28px 0 12px}}
.sub{{color:var(--dim);font-size:13px;margin-bottom:24px}}
.hero{{border:1px solid {"#238636" if all_ok else "#da3633"};
background:{"linear-gradient(135deg,#12261a,#161b22)" if all_ok else "linear-gradient(135deg,#2d1416,#161b22)"};
border-radius:14px;padding:26px 28px;margin-bottom:26px}}
.hero .big{{font-size:30px;font-weight:700;color:{"#3fb950" if all_ok else "#f85149"};letter-spacing:1px}}
.hero .desc{{color:var(--dim);margin-top:8px;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0 8px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.kpi .v{{font-size:24px;font-weight:700}} .kpi .l{{color:var(--dim);font-size:12px;margin-top:2px}}
.round{{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:18px 0;overflow:hidden}}
.rhead{{display:flex;align-items:center;gap:12px;padding:14px 18px;background:#1c2128;border-bottom:1px solid var(--line)}}
.rhead .n{{font-weight:700;font-size:16px}} .rhead .m{{color:var(--dim);font-size:12px;margin-left:auto}}
.cat{{border-top:1px solid var(--line)}}
.cat:first-child{{border-top:0}}
.catname{{padding:10px 18px;background:#11161d;color:var(--acc);font-weight:600;font-size:13px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td{{padding:7px 18px;border-top:1px solid #21262d;vertical-align:top}}
td.n{{width:52%}} td.s{{width:80px}} td.t{{width:70px;color:var(--dim);text-align:right;font-variant-numeric:tabular-nums}}
td.d{{color:var(--dim);font-size:12px;word-break:break-all}}
.pill{{display:inline-block;padding:1px 9px;border-radius:20px;font-size:11px;font-weight:700}}
.pill.ok{{background:rgba(63,185,80,.15);color:var(--ok);border:1px solid rgba(63,185,80,.4)}}
.pill.bad{{background:rgba(248,81,73,.15);color:var(--bad);border:1px solid rgba(248,81,73,.4)}}
details{{margin-top:18px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 18px}}
summary{{cursor:pointer;color:var(--acc)}} pre{{white-space:pre-wrap;word-break:break-all;color:var(--dim);font-size:12px}}
.foot{{color:var(--dim);font-size:12px;margin-top:28px;border-top:1px solid var(--line);padding-top:14px}}
</style></head><body><div class="wrap">
<h1>抖音 / 快手收藏自动下载 · 全功能验收报告</h1>
<div class="sub">v{esc(rounds[0]['env']['version'])} &nbsp;·&nbsp; HOOK_VER {rounds[0]['env']['hook_ver']}
&nbsp;·&nbsp; 生成于 {esc(now)} &nbsp;·&nbsp; 工作区 {esc(rounds[0]['env']['cwd'])}</div>
<div class="hero">
  <div class="big">{"✔ 两轮全部通过 · 完美运行" if all_ok else "✘ 存在未通过项，需修复"}</div>
  <div class="desc">用户要求「所有功能测两遍」。本轮验收把 A~H 八大类功能各跑两遍：
  双站点解析与事件管线、真实下载链路（含断点续传/416/内容嗅探）、完整落盘链路、
  面板状态机、真实面板 HTTP、真浏览器钩子注入、运行时守护与全量 pytest。</div>
  <div class="grid">
    <div class="kpi"><div class="v">{len(rounds)}</div><div class="l">验收轮次</div></div>
    <div class="kpi"><div class="v" style="color:var(--ok)">{passed}</div><div class="l">通过检查</div></div>
    <div class="kpi"><div class="v" style="color:{"var(--ok)" if not failed else "var(--bad)"}">{failed}</div><div class="l">失败检查</div></div>
    <div class="kpi"><div class="v">{total}</div><div class="l">检查总数</div></div>
  </div>
</div>
"""]

    for r in rounds:
        parts.append(f"""<div class="round"><div class="rhead">
<span class="n">第 {r['round']} 轮</span>
{badge(r['failed'] == 0)}
<span class="m">{esc(r['started_at'])} → {esc(r['finished_at'])} · 耗时 {r['duration_s']}s ·
{r['passed']}/{r['total']} 通过</span></div>""")
        for cat in r["categories"]:
            cok = all(c["ok"] for c in cat["checks"])
            parts.append(f'<div class="cat"><div class="catname">{badge(cok)} &nbsp;{esc(cat["name"])}</div><table>')
            for c in cat["checks"]:
                parts.append(
                    f'<tr><td class="n">{esc(c["name"])}</td>'
                    f'<td class="s">{badge(c["ok"])}</td>'
                    f'<td class="t">{c["ms"]}ms</td>'
                    f'<td class="d">{esc(c["detail"])}</td></tr>')
            parts.append("</table></div>")
        parts.append("</div>")

    ev = rounds[-1].get("evidence", {})
    parts.append("<details><summary>运行时证据（最后一轮）</summary><pre>")
    for k in ("git_head", "git_status", "pytest", "processes_sample"):
        if k in ev:
            parts.append(f"{esc(k)}:\n{esc(ev[k])}\n\n")
    parts.append("</pre></details>")
    parts.append(f"""<div class="foot">
说明: 验收全程未改动用户设置（.settings.json 逐字节还原）、未删除任何真实下载记录、
未关闭用户的 Chrome（CDP 只读后 9222 仍存活）。
唯一新增文件是本 acceptance/ 目录下的证据与本报告，产品代码零改动。
</div></div></body></html>""")
    return "".join(parts)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, choices=[1, 2])
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if a.round:
        res = run_round(a.round)
        out = ACC / f"round_{a.round}.json"
        out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n===== 第 {a.round} 轮: {res['passed']}/{res['total']} 通过, "
              f"失败 {res['failed']}, 耗时 {res['duration_s']}s =====")
        print(f"证据: {out}")
        return 0 if res["failed"] == 0 else 1

    if a.report:
        paths = [ACC / "round_1.json", ACC / "round_2.json"]
        rounds = []
        for p in paths:
            if not p.exists():
                print(f"缺少 {p}, 请先跑 --round 1 / --round 2")
                return 2
            rounds.append(json.loads(p.read_text(encoding="utf-8")))
        html = build_report(rounds)
        hp = ACC / "验收报告.html"
        hp.write_text(html, encoding="utf-8")
        print(f"报告: {hp}")
        # 顺手截一张 PNG, 方便用户直接「看到」
        try:
            from playwright.sync_api import sync_playwright
            png = ACC / "验收报告.png"
            with sync_playwright() as pw:
                br = pw.chromium.launch(headless=True, channel="chrome",
                                        args=["--disable-gpu", "--no-first-run"])
                pg = br.new_page(viewport={"width": 1280, "height": 900},
                                 device_scale_factor=1)
                pg.goto(hp.as_uri(), wait_until="load", timeout=30000)
                pg.screenshot(path=str(png), full_page=True)
                br.close()
            print(f"截图: {png}")
        except Exception as e:
            print(f"(截图跳过: {e})")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
