"""v2.2 分段并发下载的回归测试。

背景: CDN 一般限的是「单条连接」的速率, 不是账号速率。实测同一个
B站 448MB 4K 轨: 单连接 1.96 MB/s, 8 路并发 13.37 MB/s (6.83 倍)。
所以给 core 加了 download_parallel / download_smart。

这里用本地 HTTP 服务器真跑, 不 mock 网络层 —— 分段/偏移/断流这些
只有在真 socket 上才测得出来。
"""
from __future__ import annotations

import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from douyin_dl import core  # noqa: E402

# 8MB 整, 内容可校验(每个位置的值由索引决定)
BIG = bytes((i * 31 + 7) & 0xFF for i in range(8 << 20))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # 每个请求记一下, 用来断言真的并发了
    hits: list[tuple[int, int]] = []
    lock = threading.Lock()
    slow = 0.0

    def log_message(self, *a):
        pass

    def _range(self):
        rng = self.headers.get("Range") or ""
        m = re.search(r"bytes=(\d+)-(\d*)", rng)
        if not m:
            return None
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else len(BIG) - 1
        return start, min(end, len(BIG) - 1)

    def do_GET(self):
        p = urlparse(self.path).path
        rg = self._range()
        with _Handler.lock:
            _Handler.hits.append(rg or (-1, -1))

        if _Handler.slow:
            time.sleep(_Handler.slow)

        if p == "/norange":
            # 明确不支持 Range 的服务器
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(BIG)))
            self.end_headers()
            try:
                self.wfile.write(BIG)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if rg is None:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(BIG)))
            self.end_headers()
            try:
                self.wfile.write(BIG)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        start, end = rg
        body = BIG[start:end + 1]
        self.send_response(206)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range",
                         f"bytes {start}-{end}/{len(BIG)}")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture()
def server():
    _Handler.hits = []
    _Handler.slow = 0.0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_seg_plan_covers_whole_file_without_gaps():
    """分段表必须无缝覆盖 [0, total), 不能有洞也不能重叠。"""
    total = 10_000_000
    plan = core._seg_plan(total, 8)
    assert plan[0][0] == 0
    assert plan[-1][1] == total - 1
    for (s1, e1), (s2, e2) in zip(plan, plan[1:]):
        assert e1 + 1 == s2, "段之间必须有洞或重叠了"
    assert sum(e - s + 1 for s, e in plan) == total


def test_seg_plan_respects_min_segment():
    """段不能切得比 PARALLEL_MIN_SEG 还碎(否则建连开销比下载还大)。"""
    plan = core._seg_plan(core.PARALLEL_MIN_SIZE, 64)
    for s, e in plan:
        assert e - s + 1 >= core.PARALLEL_MIN_SEG or len(plan) == 1


def test_parallel_download_assembles_correct_bytes(tmp_path, server):
    """核心契约: 分段并发下出来的文件必须与源逐字节一致。"""
    dest = tmp_path / "out.mp4"
    sess = core.build_session()
    ok = core.download_parallel(
        sess, f"{server}/file", dest, workers=8, log=lambda *a: None)
    assert ok is True
    assert dest.exists()
    assert dest.read_bytes() == BIG, "分段拼装后内容不对, 偏移算错了"


def test_parallel_download_really_uses_multiple_connections(tmp_path, server):
    """必须真的开了多条连接: 服务器端应该收到 1 次探长 + N 段 Range 请求。"""
    dest = tmp_path / "out.mp4"
    sess = core.build_session()
    workers = 8
    core.download_parallel(sess, f"{server}/file", dest, workers=workers,
                           log=lambda *a: None)
    expect = len(core._seg_plan(len(BIG), workers))
    assert expect >= 2, "这个样本太小, 本来就切不出多段"
    # 探长请求是 bytes=0-0 -> (0, 0); 各段是 (start, end) 且 end > start
    ranged = [h for h in _Handler.hits if h[0] >= 0 and h[1] > h[0]]
    assert len(ranged) >= expect, (
        f"应该切 {expect} 段, 实际看到 {len(ranged)} 个分段请求")


def test_parallel_download_reports_monotonic_progress(tmp_path, server):
    """进度回调必须单调递增, 最后一条等于总长(面板进度条依赖这个)。"""
    seen = []
    dest = tmp_path / "out.mp4"
    sess = core.build_session()
    core.download_parallel(
        sess, f"{server}/file", dest, workers=8,
        log=lambda *a: None,
        on_progress=lambda d, t: seen.append((d, t)))
    assert seen, "一次进度都没报"
    assert seen[-1][0] == len(BIG) and seen[-1][1] == len(BIG)
    dones = [d for d, _ in seen]
    assert dones == sorted(dones), "进度回退了"


def test_parallel_download_fails_when_server_has_no_range(tmp_path, server):
    """服务端不支持 Range 时必须返回 False, 好让上层退回单连接。"""
    dest = tmp_path / "out.mp4"
    sess = core.build_session()
    ok = core.download_parallel(
        sess, f"{server}/norange", dest, workers=8, log=lambda *a: None)
    assert ok is False
    assert not dest.exists()
    assert not dest.with_suffix(".mp4.part").exists(), "失败后不该留下 .part"


def test_smart_falls_back_to_single_stream_for_small_file(tmp_path, monkeypatch):
    """小文件不该走并发(建连开销比下载还大), 应直接落到 download。"""
    called = {"parallel": 0, "single": 0}

    def _fake_parallel(*a, **k):
        called["parallel"] += 1
        return True

    def _fake_single(*a, **k):
        called["single"] += 1
        return True

    monkeypatch.setattr(core, "download_parallel", _fake_parallel)
    monkeypatch.setattr(core, "download", _fake_single)
    monkeypatch.setattr(core, "probe_size", lambda s, u: 1 << 20)   # 1MB

    ok = core.download_smart(None, "http://x/y", tmp_path / "a.mp4",
                             log=lambda *a: None)
    assert ok is True
    assert called == {"parallel": 0, "single": 1}


def test_smart_uses_parallel_for_big_file(tmp_path, monkeypatch):
    called = {"parallel": 0, "single": 0}
    monkeypatch.setattr(core, "download_parallel",
                        lambda *a, **k: (called.__setitem__("parallel", 1), True)[1])
    monkeypatch.setattr(core, "download",
                        lambda *a, **k: (called.__setitem__("single", 1), True)[1])
    monkeypatch.setattr(core, "probe_size", lambda s, u: 64 << 20)  # 64MB

    ok = core.download_smart(None, "http://x/y", tmp_path / "a.mp4",
                             log=lambda *a: None, workers=8)
    assert ok is True
    assert called == {"parallel": 1, "single": 0}


def test_smart_falls_back_when_parallel_fails(tmp_path, monkeypatch):
    """分段失败必须自动退回单连接, 不能整条视频就废了。"""
    called = {"single": 0}
    monkeypatch.setattr(core, "download_parallel", lambda *a, **k: False)
    monkeypatch.setattr(core, "download",
                        lambda *a, **k: (called.__setitem__("single", 1), True)[1])
    monkeypatch.setattr(core, "probe_size", lambda s, u: 64 << 20)

    ok = core.download_smart(None, "http://x/y", tmp_path / "a.mp4",
                             log=lambda *a: None)
    assert ok is True
    assert called["single"] == 1


def test_smart_respects_workers_zero(monkeypatch, tmp_path):
    """workers=0/1 表示用户关掉并发, 必须完全走单连接。"""
    called = {"parallel": 0, "single": 0}
    monkeypatch.setattr(core, "download_parallel",
                        lambda *a, **k: (called.__setitem__("parallel", 1), True)[1])
    monkeypatch.setattr(core, "download",
                        lambda *a, **k: (called.__setitem__("single", 1), True)[1])
    monkeypatch.setattr(core, "probe_size", lambda s, u: 64 << 20)

    core.download_smart(None, "http://x/y", tmp_path / "a.mp4",
                        log=lambda *a: None, workers=0)
    assert called == {"parallel": 0, "single": 1}


def test_dl_workers_setting_is_clamped():
    """面板传进来的连接数必须夹在 0~16, 坏值回落到 8。"""
    from douyin_dl.ui import _clamp_workers
    assert _clamp_workers(None) == 8
    assert _clamp_workers(12) == 12
    assert _clamp_workers("12") == 12
    assert _clamp_workers(99) == 16
    assert _clamp_workers(-5) == 0
    assert _clamp_workers("abc") == 8
