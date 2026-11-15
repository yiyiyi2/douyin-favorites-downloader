# -*- coding: utf-8 -*-
"""v2.6 回归: SSE 增量推送。

背景: 面板每 0.4 秒推一次整包快照, 实测 114 KB, 其中 recent(最近 300 条)
占 113.4 KB / 99%, 其余标量合计才 0.5 KB。下载时等于每分钟纯为历史记录
重传 ~6.7 MB, 浏览器还要重新 JSON.parse + 重绘。

改法: 首帧发 {full: snap}, 之后每帧只发 {patch: 变化的键}。历史没变就不发。

这个文件锁住四件事:
  1. _diff_snap 只报变化的键, 没变化报空 dict
  2. recent 没变时不进 patch(否则等于没优化), 变了必须进
  3. SSE 首帧是 full, 后续是 patch, 且 patch 拼起来等于最新快照
  4. 前端 onmessage 能同时吃 full / patch / 旧整包三种格式
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from douyin_dl import ui  # noqa: E402

WEB = ROOT / "douyin_dl" / "web"


# ----------------------------------------------------------------- 造数据
def _workspace(tmp: Path, n: int = 40) -> Path:
    """造一个有 n 条历史记录的工作区(带 _meta, 让 recent 有真实体积)。"""
    dl = tmp / "downloads"
    (dl / "_meta").mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    state = {}
    for i in range(n):
        aid = f"77000000000{i:06d}"
        state[aid] = {"file": f"downloads/x_{aid}.mp4", "quality": "1920x1080", "time": now - i}
        (dl / "_meta" / f"{aid}.json").write_text(json.dumps({
            "aweme_id": aid, "desc": "标题" * 10, "author": "作者", "create_time": now,
            "duration_ms": 13300, "music": "音乐", "file": f"x_{aid}.mp4",
            "quality": "1920x1080",
            "cover": "https://p3.douyinpic.com/aweme/1080x1920/abcdefghijklmn.jpg",
        }, ensure_ascii=False), encoding="utf-8")
    (tmp / ".state.json").write_text(json.dumps(state), encoding="utf-8")
    (tmp / ".settings.json").write_text("{}", encoding="utf-8")
    return tmp


def _bus(tmp: Path) -> ui.EventBus:
    return ui.EventBus(tmp, tmp / "downloads", tmp / ".state.json", tmp / "log.txt")


# ------------------------------------------------------- _diff_snap 基本语义
def test_diff_returns_empty_when_nothing_changed():
    a = {"now": 1, "total": 2, "recent": [{"id": "1"}]}
    assert ui._diff_snap(a, dict(a)) == {}


def test_diff_reports_only_changed_scalars():
    a = {"now": 1, "total": 2, "quality": "max"}
    b = {"now": 9, "total": 2, "quality": "max"}
    assert ui._diff_snap(a, b) == {"now": 9}


def test_diff_reports_new_and_removed_keys():
    a = {"now": 1, "gone": "x"}
    b = {"now": 1, "fresh": "y"}
    assert ui._diff_snap(a, b) == {"fresh": "y", "gone": None}


def test_diff_handles_non_dict_prev():
    assert ui._diff_snap(None, {"a": 1}) == {"a": 1}
    assert ui._diff_snap("junk", {"a": 1}) == {"a": 1}


# ------------------------------------------------- recent 是优化的关键点
def test_diff_skips_recent_when_same_list_object():
    """_recent() 有缓存, 没变时返回同一个 list 对象 —— 必须零成本跳过。"""
    recent = [{"id": str(i), "title": "x" * 50} for i in range(300)]
    a = {"now": 1, "recent": recent}
    b = {"now": 2, "recent": recent}          # 同一个对象
    patch = ui._diff_snap(a, b)
    assert "recent" not in patch
    assert patch == {"now": 2}


def test_diff_includes_recent_when_content_changed():
    a = {"now": 1, "recent": [{"id": "1"}]}
    b = {"now": 1, "recent": [{"id": "1"}, {"id": "2"}]}
    assert "recent" in ui._diff_snap(a, b)


def test_progress_only_frame_is_tiny(tmp_path):
    """真实的进度帧(只有 counters/active 变)必须很小 —— 这是本优化的目的。"""
    tmp = _workspace(tmp_path, n=300)
    bus = _bus(tmp)
    base = bus.snapshot()
    full = len(json.dumps({"full": base}, ensure_ascii=False).encode("utf-8"))
    assert full > 50_000, f"造的 recent 太小, 测不出压缩效果: {full}"

    nxt = dict(base)
    nxt["counters"] = {"ok": 7, "skip": 0, "fail": 0}
    nxt["active"] = [{"id": "x", "pct": 33.0}]
    patch = ui._diff_snap(base, nxt)
    assert set(patch) == {"counters", "active"}
    small = len(json.dumps({"patch": patch}, ensure_ascii=False).encode("utf-8"))
    assert small < 500, f"进度帧应该是几百字节级别, 实际 {small}"
    assert full / max(1, small) > 100, f"压缩比不达标: {full}/{small}"


# --------------------------------------------------------------- HTTP 端到端
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _read_sse_events(port: int, want: int, sink: list | None = None, timeout: float = 20.0) -> list[dict]:
    """直连 SSE, 收 want 个 data 事件就断开。sink 非空则逐条往里 append(便于
    主线程在收满 want 条之前就看到进度)。"""
    out: list[dict] = []
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/events")
    resp = urllib.request.urlopen(req, timeout=timeout)
    try:
        buf = b""
        deadline = time.time() + timeout
        while len(out) < want and time.time() < deadline:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                for line in raw.split(b"\n"):
                    if line.startswith(b"data:"):
                        ev = json.loads(line[5:].decode("utf-8"))
                        out.append(ev)
                        if sink is not None:
                            sink.append(ev)
    finally:
        resp.close()
    return out


def test_sse_first_frame_is_full_then_patches(tmp_path):
    """首帧必须是 {full}, 后续是 {patch}; patch 逐步合并后 state 跟着变。"""
    tmp = _workspace(tmp_path, n=20)
    bus = _bus(tmp)
    port = _free_port()
    ui.start_ui(bus, WEB / "index.html", port=port, open_browser=False, log=lambda *_: None)
    time.sleep(0.4)
    try:
        evs: list[dict] = []

        def collect():
            _read_sse_events(port, want=4, sink=evs, timeout=15)

        t = threading.Thread(target=collect, daemon=True)
        t.start()
        # 等首帧到达再制造变化
        for _ in range(60):
            if evs:
                break
            time.sleep(0.1)
        assert evs, "没收到任何 SSE 帧"
        assert "full" in evs[0], f"首帧不是 full: {list(evs[0])[:4]}"
        assert "recent" in evs[0]["full"], "首帧 full 里应该带 recent"

        state = evs[0]["full"]
        bus.set_status(running=True, note="测试中")
        bus.counters["ok"] += 1
        bus.publish()
        t.join(timeout=16)

        patches = [e["patch"] for e in evs[1:] if isinstance(e, dict) and "patch" in e]
        assert patches, f"没收到 patch 帧: {[list(e)[:2] for e in evs[1:]]}"
        merged = dict(state)
        for p in patches:
            merged.update(p)
        assert merged["status"].get("note") == "测试中", merged["status"]
        assert merged["counters"]["ok"] == state["counters"]["ok"] + 1
        # 进度类更新不该带 recent
        for p in patches:
            assert "recent" not in p, f"历史没变却重传了 recent: {list(p)}"
    finally:
        pass


def test_sse_ping_still_sent_when_idle(tmp_path):
    """空闲 10 秒要发 ping 保活 —— 改增量时不能把这条丢了。"""
    tmp = _workspace(tmp_path, n=5)
    bus = _bus(tmp)
    port = _free_port()
    ui.start_ui(bus, WEB / "index.html", port=port, open_browser=False, log=lambda *_: None)
    time.sleep(0.3)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/events")
    resp = urllib.request.urlopen(req, timeout=30)
    try:
        buf = b""
        deadline = time.time() + 16
        while time.time() < deadline:
            buf += resp.read(1)
            if b": ping" in buf:
                break
        assert b": ping" in buf, "空闲 16 秒没等到 ping 保活"
    finally:
        resp.close()


# --------------------------------------------------------------- 前端契约
def _panel_js() -> str:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    import re
    body = "\n".join(re.findall(r"<script>(.*?)</script>", html, flags=re.S))
    # 先剥注释, 否则会命中我们自己的说明文字
    return re.sub(r"//[^\n]*", "", body)


def test_frontend_understands_full_patch_and_legacy():
    js = _panel_js()
    assert "d.full" in js, "前端没处理 {full:...} 首帧"
    assert "d.patch" in js, "前端没处理 {patch:...} 增量帧"
    # 老格式(直接一个对象)仍然要吃得下, 否则缓存了旧 html 的浏览器会白屏
    assert "STATE = d" in js, "前端丢了旧整包格式的兼容"
    # 合并而不是替换
    assert "for(const k in p)" in js or "for (const k in p)" in js


def test_backend_emits_full_then_patch():
    src = (ROOT / "douyin_dl" / "ui.py").read_text(encoding="utf-8")
    assert '{"full": base}' in src, "后端没发 {full}"
    assert '{"patch": patch}' in src, "后端没发 {patch}"
    assert "_diff_snap(" in src
