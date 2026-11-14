"""v2.2.1 回归: 所有子进程都必须带 CREATE_NO_WINDOW, 不许弹黑框。

用户报的原话: "在b站下载的时候, 刚下完会突然弹出一个命令行, 然后很快就没了"。
根因: 主程序是 pythonw.exe 起的(没有控制台), 这种进程里 spawn 任何子进程,
Windows 都会给它新开一个控制台窗口。B站下完要调 ffmpeg 合并音视频, 于是闪一下。

这里不测"窗口真的没出现"(那要开桌面会话), 而是锁住契约: 每个调用点都传了
CREATE_NO_WINDOW。回归一旦被人改回去, 这个测试立刻红。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from douyin_dl import core  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows 专有")


def test_no_window_flag_exists_on_windows():
    assert core._NO_WINDOW != 0, "CREATE_NO_WINDOW 没取到"
    assert core._NO_WINDOW == subprocess.CREATE_NO_WINDOW


def test_run_hidden_injects_creationflags(monkeypatch):
    """run_hidden 必须自己补 creationflags, 调用方不用操心。"""
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    core.run_hidden(["x", "y"], capture_output=True)
    assert seen["creationflags"] == core._NO_WINDOW


def test_run_hidden_keeps_caller_flags(monkeypatch):
    """调用方自己指定了 creationflags 就别覆盖。"""
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    core.run_hidden(["x"], creationflags=0xDEAD)
    assert seen["creationflags"] == 0xDEAD


def test_popen_hidden_injects_creationflags(monkeypatch):
    seen = {}

    class _FakePopen:
        def __init__(self, cmd, **kw):
            seen.update(kw)
            seen["cmd"] = cmd

    monkeypatch.setattr(core.subprocess, "Popen", _FakePopen)
    core.popen_hidden(["x"], stdout=subprocess.DEVNULL)
    assert seen["creationflags"] == core._NO_WINDOW


# --------------------------------------------------------------------------
# 静态扫描: 源码里不许再出现裸的 subprocess.run / subprocess.Popen
# --------------------------------------------------------------------------

_SCAN = ["douyin_dl/core.py", "douyin_dl/bili.py", "douyin_dl/watcher.py",
         "douyin_dl/ui.py", "douyin_dl/daemon.py", "douyin_dl.py"]
# 这两个文件里的裸调用是"定义本身"或"已经自己加了 flags 的"
_ALLOW = {
    ("douyin_dl/core.py", "run"),
    ("douyin_dl/core.py", "Popen"),
    ("douyin_dl/daemon.py", "Popen"),   # 自己拼了 CREATE_NO_WINDOW
    ("douyin_dl/watcher.py", "Popen"),  # 只是类型注解
}


def _raw_subprocess_calls(path: Path):
    """找出裸 subprocess.xxx(...) 调用中**没有**传 creationflags 的。

    自己显式传了 creationflags 的算安全, 不需要再经 helper。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "subprocess"
                and f.attr in ("run", "Popen", "call", "check_output", "check_call")):
            continue
        has_flags = any(k.arg == "creationflags" for k in node.keywords)
        if has_flags:
            continue
        out.append((f.attr, node.lineno))
    return out


def test_no_bare_subprocess_calls_left():
    """除了 helper 自己和显式加了 flags 的, 别处一律要走 run_hidden/popen_hidden。"""
    bad = []
    for rel in _SCAN:
        p = ROOT / rel
        if not p.exists():
            continue
        for attr, line in _raw_subprocess_calls(p):
            if (rel, attr) in _ALLOW:
                continue
            bad.append(f"{rel}:{line} subprocess.{attr}(...)")
    assert not bad, (
        "这些调用没传 creationflags, pythonw 下会弹黑框:\n  "
        + "\n  ".join(bad))


def test_ffmpeg_merge_uses_hidden(monkeypatch, tmp_path):
    """B站合并音视频这条路径必须走 core.run_hidden —— 用户就是在这里看到闪框的。"""
    from douyin_dl import bili

    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["kw"] = kw
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bili, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(core.subprocess, "run", fake_run)
    dest = tmp_path / "o.mp4"
    dest.write_bytes(b"x")
    bili.merge_av(tmp_path / "v.mp4", tmp_path / "a.mp4", dest,
                  log=lambda *a: None)
    assert "creationflags" in calls["kw"], "merge_av 没传 creationflags"
    assert calls["kw"]["creationflags"] == core._NO_WINDOW
