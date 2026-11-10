# -*- coding: utf-8 -*-
"""
端到端: 真 Chrome + HOOK_JS + expose_binding。
不要求登录 —— 只验证「页面发出的真实请求能被钩子捕获并送到 Python」。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from douyin_dl.watcher import BrowserWatcher, HOOK_JS

PROFILE = Path(__file__).resolve().parent.parent / ".chrome_profile_test"
PORT = 9355


def main():
    w = BrowserWatcher(PROFILE, port=PORT)
    w.download_all_on_start = True     # 只为了看 _on_payload 有没有被走到
    logs = []
    w.log = lambda *a, **k: logs.append(" ".join(str(x) for x in a))

    hooked = []
    orig = w._on_payload

    def spy(kind, url, body, req):
        hooked.append((kind, url.split("?")[0], len(body)))
        orig(kind, url, body, req)

    w._on_payload = spy

    if not w.start_chrome():
        print("FAIL: 浏览器没起来")
        return 1

    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
            ctx = b.contexts[0] if b.contexts else b.new_context()
            w._ctx = ctx
            ctx.expose_binding("__dyReport", w._on_binding)
            ctx.add_init_script(HOOK_JS)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            w._page = page

            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            w._install_hook(page)
            assert page.evaluate("() => !!window.__DY_HOOKED_V2__"), "钩子没装上"
            print("钩子已装")

            # 页面真实发两条被关心的请求
            page.evaluate("""async () => {
                try { await (await fetch('/aweme/v1/web/aweme/detail/?aweme_id=7300000000000000001')).text(); } catch(e){}
            }""")
            page.evaluate("""() => { try {
                const x = new XMLHttpRequest();
                x.open('GET', '/aweme/v1/web/aweme/listcollection/?count=1');
                x.send();
            } catch(e){} }""")

            for _ in range(12):
                w.pump(ctx)
                w.drain()
                time.sleep(0.5)
            page.close()
    finally:
        if w.proc:
            w.proc.terminate()

    print(f"\nbinding 收到 {w.seen_payloads} 条, 分类后的事件:")
    for k, u, n in hooked:
        print(f"  [{k}] {u[:70]}  {n}B")

    kinds = {k for k, _, _ in hooked}
    assert w.seen_payloads >= 1, "binding 一条都没收到"
    assert "data" in kinds, f"没有 data 事件, 实际 {kinds}"
    print("\n=== e2e: 页面请求 -> 钩子 -> Python 链路正常 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
