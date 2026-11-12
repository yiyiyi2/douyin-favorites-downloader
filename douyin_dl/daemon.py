# -*- coding: utf-8 -*-
"""
守护模式: douyin_dl.py 挂了就自动拉起来。

用法(一般由 启动收藏下载.bat 调):
    python douyin_dl/daemon.py            # 前台守护
    pythonw douyin_dl/daemon.py           # 无黑窗口守护

逻辑:
    起一个子进程跑 douyin_dl.py;
    每秒看一眼 —— 子进程退出就等 5 秒重新拉起;
    连续 5 次秒退(每次活不过 60 秒)就放弃, 避免死循环刷日志。
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
MAIN = WORKSPACE / "douyin_dl.py"
# 守护自己的日志单独放一份, 别和主程序的 douyin_dl.log 抢(那个文件主程序会做 5MB 轮转)
DAEMON_LOG = WORKSPACE / "daemon.log"

# 活不过这个秒数算「秒退」, 连续秒退太多次就放弃
RAPID_EXIT_SECS = 60
MAX_RAPID = 5
RESTART_DELAY = 5
# 下载进程用这个退出码表示「用户主动关闭, 不要拉起」(如面板里关了自动重开、用户关掉全部抖音页)
EXIT_INTENTIONAL = 7


def _log(msg: str) -> None:
    """写一行带时间戳的日志到 daemon.log。

    守护是被 pythonw.exe 拉起的: pythonw 下 sys.stdout is None,
    裸 print 会直接抛异常 / 静默丢失, 秒退重启日志一行都看不到。
    所以日志必须落盘; 整个写文件用 try/except 兜住 —— 日志本身
    失败(写不进去、磁盘满等)也绝不能让守护崩。
    """
    try:
        with open(DAEMON_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass
    # 前台 python 启动时顺便留一份到控制台; pythonw 下 stdout 为空就跳过
    try:
        if sys.stdout is not None:
            print(msg, flush=True)
    except Exception:
        pass


def _python_exe() -> str:
    return sys.executable or "python"


def guard():
    rapid = 0
    n = 0
    _log(f"[daemon] 守护启动, 目标: {MAIN}")
    while True:
        n += 1
        _log(f"[daemon] 第 {n} 次启动下载进程…")
        t0 = time.time()
        try:
            # v1.6: 把 启动收藏下载.bat 的 %* 透传给主程序。
            # 以前写死 "--no-open", 用户加的 --check/--port/--output 全被吞掉,
            # bat 里加了 %* 也没用(参数只到守护, 到不了 douyin_dl.py)。
            # 守护自己不需要任何参数, 所以 sys.argv[1:] 整份转下去;
            # --no-open 必须保留(守护拉起时别弹浏览器, 面板标签会自动重连),
            # 用户重复给了也无妨(argparse 的 store_true 不怕重复)。
            _user_args = [a for a in sys.argv[1:] if a != "--no-open"]
            _child = [_python_exe(), str(MAIN), *_user_args, "--no-open"]
            proc = subprocess.Popen(
                _child,
                cwd=str(WORKSPACE),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        except Exception as e:
            _log(f"[daemon] 启动失败: {e}")
            time.sleep(RESTART_DELAY)
            continue

        try:
            code = proc.wait()
        except KeyboardInterrupt:
            _log("[daemon] 收到退出信号, 关闭下载进程…")
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
            return

        lived = time.time() - t0
        _log(f"[daemon] 下载进程退出 (code={code}, 存活 {lived:.0f}s)")

        if code == EXIT_INTENTIONAL:
            _log("[daemon] 用户主动关闭(关掉了抖音且自动重开=关), 守护退出。"
                 "下次双击 启动收藏下载.bat 再开始。")
            return

        if lived >= RAPID_EXIT_SECS:
            rapid = 0                      # 正常跑过一段时间的退出不算秒退
        else:
            rapid += 1
            if rapid >= MAX_RAPID:
                _log(f"[daemon] 连续 {rapid} 次秒退, 放弃守护。"
                     f"请看 douyin_dl.log 里最新的报错。")
                return
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    try:
        guard()
    except KeyboardInterrupt:
        pass
