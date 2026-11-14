"""v2.3 性能优化的回归测试。

锁住三件事:
1. `_rel_path` 换成纯字符串判定后, **路径穿越防护一点没松**(这是安全底线);
2. `_recent()` 的"元信息签名"缓存只有在 .state.json / _meta 真的变了才失效,
   该刷新的场景一个都不能漏(否则面板显示过期数据);
3. 目录清单缓存(`_lookup_file`)不会把已删文件当成还在、也不会漏掉新文件。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from douyin_dl.ui import EventBus, _rel_path


def _bus(tmp: Path) -> EventBus:
    return EventBus(workspace=tmp, out_dir=tmp / "downloads",
                    state_file=tmp / ".state.json", log_file=tmp / "log")


def _write_state(tmp: Path, rows: dict):
    (tmp / ".state.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 安全
@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "..\\..\\windows\\system32\\config",
    "a/../../b",
    "downloads/../../../x",
    "./..",
    "x/./../../y",
    "..",
    "../",
    "a/..",
])
def test_rel_path_still_blocks_traversal_after_optimization(bad, tmp_path):
    """纯字符串判定必须和原来的 resolve() 判定一样严 —— 一个都不能放行。"""
    ws = tmp_path.resolve()
    assert _rel_path(ws, bad) is None


@pytest.mark.parametrize("junk", ["", "   ", None, 123, 3.5, {"x": 1}, ["a"], True])
def test_rel_path_rejects_non_path_junk(junk, tmp_path):
    assert _rel_path(tmp_path, junk) is None


@pytest.mark.parametrize("good", [
    "downloads/a.mp4",
    "downloads/sub/b.mp4",
    "./downloads/c.mp4",
    "downloads\\d.mp4",
    "a/b/c/d/e.mp4",
])
def test_rel_path_accepts_safe_relative(good, tmp_path):
    ws = tmp_path.resolve()
    p = _rel_path(ws, good)
    assert p is not None
    assert str(p).startswith(str(ws))


def test_rel_path_keeps_absolute_paths(tmp_path):
    """自定义下载目录时 state 里存的是绝对路径, 必须放行。"""
    other = tmp_path / "elsewhere" / "v.mp4"
    assert _rel_path(tmp_path, str(other)) == other


# ---------------------------------------------------------------- 缓存正确性
def test_recent_cache_hits_when_nothing_changed(tmp_path):
    _write_state(tmp_path, {"7100000000000000001": {"file": "downloads/a.mp4", "time": 100}})
    bus = _bus(tmp_path)
    first = bus._recent()
    assert len(first) == 1
    # 第二次必须走缓存(同一个 list 对象)
    assert bus._recent() is first


def test_recent_cache_invalidates_when_state_file_changes(tmp_path):
    _write_state(tmp_path, {"7100000000000000001": {"file": "downloads/a.mp4", "time": 100}})
    bus = _bus(tmp_path)
    assert len(bus._recent()) == 1
    # 新增一条 -> 必须重新解析出来
    _write_state(tmp_path, {
        "7100000000000000001": {"file": "downloads/a.mp4", "time": 100},
        "7100000000000000002": {"file": "downloads/b.mp4", "time": 200},
    })
    assert len(bus._recent()) == 2


def test_recent_cache_invalidates_when_meta_appears(tmp_path):
    """_meta 里新出现一个 json -> 标题必须补上, 不能一直吃旧缓存。"""
    _write_state(tmp_path, {"7100000000000000001": {"file": "downloads/a.mp4", "time": 100}})
    bus = _bus(tmp_path)
    assert bus._recent()[0]["title"] == ""
    meta = tmp_path / "downloads" / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "7100000000000000001.json").write_text(
        json.dumps({"desc": "标题来了", "author": "作者"}, ensure_ascii=False), encoding="utf-8")
    got = bus._recent()
    assert got[0]["title"] == "标题来了"
    assert got[0]["author"] == "作者"


def test_finish_does_not_force_full_rescan(tmp_path):
    """finish() 只把签名置空, 不该把缓存整个丢掉 —— 否则每下完一个就全量重扫。"""
    _write_state(tmp_path, {"7100000000000000001": {"file": "downloads/a.mp4", "time": 100}})
    bus = _bus(tmp_path)
    bus._recent()
    cached = bus._recent_cache
    assert cached is not None
    bus.finish("7100000000000000001", True)
    assert bus._recent_cache is cached        # 缓存对象仍在


# ---------------------------------------------------------------- 目录清单
def test_lookup_file_uses_listing_and_sees_size(tmp_path):
    d = tmp_path / "downloads"
    d.mkdir()
    f = d / "a.mp4"
    f.write_bytes(b"x" * 1234)
    bus = _bus(tmp_path)
    info = bus._lookup_file(f)
    assert info is not None
    assert info == (1234, False)


def test_lookup_file_reports_missing(tmp_path):
    d = tmp_path / "downloads"
    d.mkdir()
    bus = _bus(tmp_path)
    assert bus._lookup_file(d / "nope.mp4") is None


def test_lookup_file_sees_directory(tmp_path):
    d = tmp_path / "downloads"
    (d / "图文作品").mkdir(parents=True)
    bus = _bus(tmp_path)
    info = bus._lookup_file(d / "图文作品")
    assert info is not None and info[1] is True


def test_lookup_file_notices_deleted_file(tmp_path):
    """删掉文件后目录 mtime 会变, 目录清单必须重建, 不能还报"存在"。"""
    d = tmp_path / "downloads"
    d.mkdir()
    f = d / "a.mp4"
    f.write_bytes(b"x" * 10)
    bus = _bus(tmp_path)
    assert bus._lookup_file(f) is not None
    # 确保 mtime 真的变了(有些文件系统精度是秒级)
    f.unlink()
    os.utime(d, (time.time() + 5, time.time() + 5))
    assert bus._lookup_file(f) is None


def test_lookup_file_notices_new_file(tmp_path):
    d = tmp_path / "downloads"
    d.mkdir()
    bus = _bus(tmp_path)
    assert bus._lookup_file(d / "new.mp4") is None
    (d / "new.mp4").write_bytes(b"y" * 99)
    os.utime(d, (time.time() + 5, time.time() + 5))
    info = bus._lookup_file(d / "new.mp4")
    assert info is not None and info[0] == 99


def test_recent_per_frame_checks_each_dir_once(tmp_path, monkeypatch):
    """一帧里 443 条记录落在同一目录, 目录 mtime 只该 stat 一次。"""
    d = tmp_path / "downloads"
    d.mkdir()
    rows = {}
    for i in range(60):
        (d / f"v{i}.mp4").write_bytes(b"x")
        rows[f"71000000000000{i:04d}"] = {"file": f"downloads/v{i}.mp4", "time": i}
    _write_state(tmp_path, rows)
    bus = _bus(tmp_path)

    calls = {"n": 0}
    real_stat = Path.stat

    def counting_stat(self, *a, **kw):
        # 只数对 downloads 目录本身的 stat
        if str(self).lower().endswith("downloads"):
            calls["n"] += 1
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", counting_stat)
    got = bus._recent()
    assert len(got) == 60
    assert all(x["size"] == 1 for x in got)
    assert calls["n"] <= 3, f"downloads 目录被 stat 了 {calls['n']} 次, 应该 <= 3"
