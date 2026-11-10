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
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
MAIN = WORKSPACE / "douyin_dl.py"

# 活不过这个秒数算「秒退」, 连续秒退太多次就放弃
RAPID_EXIT_SECS = 60
MAX_RAPID = 5
RESTART_DELAY = 5
# 下载进程用这个退出码表示「用户主动关闭, 不要拉起」(如面板里关了自动重开、用户关掉全部抖音页)
EXIT_INTENTIONAL = 7


def _python_exe() -> str:
    return sys.executable or "python"


def guard():
    rapid = 0
    n = 0
    print(f"[daemon] 守护启动, 目标: {MAIN}", flush=True)
    while True:
        n += 1
        print(f"[daemon] 第 {n} 次启动下载进程…", flush=True)
        t0 = time.time()
        try:
            proc = subprocess.Popen(
                [_python_exe(), str(MAIN), "--no-open"],   # 守护拉起不弹面板浏览器, 面板标签还开着会自动重连
                cwd=str(WORKSPACE),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        except Exception as e:
            print(f"[daemon] 启动失败: {e}", flush=True)
            time.sleep(RESTART_DELAY)
            continue

        try:
            code = proc.wait()
        except KeyboardInterrupt:
            print("[daemon] 收到退出信号, 关闭下载进程…", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
            return

        lived = time.time() - t0
        print(f"[daemon] 下载进程退出 (code={code}, 存活 {lived:.0f}s)", flush=True)

        if code == EXIT_INTENTIONAL:
            print("[daemon] 用户主动关闭(关掉了抖音且自动重开=关), 守护退出。"
                  "下次双击 启动收藏下载.bat 再开始。", flush=True)
            return

        if lived >= RAPID_EXIT_SECS:
            rapid = 0                      # 正常跑过一段时间的退出不算秒退
        else:
            rapid += 1
            if rapid >= MAX_RAPID:
                print(f"[daemon] 连续 {rapid} 次秒退, 放弃守护。"
                      f"请看 douyin_dl.log 里最新的报错。", flush=True)
                return
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    try:
        guard()
    except KeyboardInterrupt:
        pass
