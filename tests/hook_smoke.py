# -*- coding: utf-8 -*-
"""HOOK_JS 真实浏览器注入冒烟测试(可独立运行, 也可被 pytest 通过子进程调用)。

为什么单独一个脚本: playwright 的 sync API 在 pytest-asyncio 的事件循环里
会报 "Cannot use sync API inside asyncio event loop", 所以 test_kuaishou.py
用 subprocess 起一个干净进程跑本脚本。

完全离线: 所有请求(含主文档)都由 page.route 本地 fulfill, 不碰快手服务器。
独立临时 profile, 绝不碰工作区 .chrome_profile。

退出码: 0=全过  1=有断言失败  2=环境不具备(系统 Chrome 不可用, 调用方应 skip)
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from douyin_dl.watcher import HOOK_JS  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except Exception as e:  # pragma: no cover
    print(f"SKIP: playwright 不可用: {e}")
    raise SystemExit(2)

REPORTS = []


def on_report(source, payload):
    REPORTS.append(json.loads(payload))


def main():
    results = []
    profile = Path(tempfile.mkdtemp(prefix="dy_hook_smoke_"))
    try:
        try:
            br = sync_playwright().start().chromium.launch(
                headless=True, channel="chrome",
                args=["--disable-blink-features=AutomationControlled", "--mute-audio",
                      "--no-first-run", "--disable-gpu"],
            )
        except Exception as e:
            print(f"SKIP: 系统 Chrome 起不来: {str(e)[:200]}")
            return 2
        try:
            ctx = br.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"))
            ctx.expose_binding("__dyReport", on_report)
            ctx.add_init_script(HOOK_JS)
            page = ctx.new_page()

            def route_handler(route):
                req = route.request
                u = req.url
                if req.method == "OPTIONS":
                    route.fulfill(status=200, headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                        "Access-Control-Allow-Headers": "*"})
                elif "/rest/v/photo/collect" in u:
                    route.fulfill(status=200, content_type="application/json",
                                  body='{"result":1}')
                elif "/rest/v/feed/hot" in u:
                    route.fulfill(status=200, content_type="application/json",
                                  body='{"result":1,"feeds":[]}')
                elif u.startswith("https://www.kuaishou.com/"):
                    route.fulfill(status=200, content_type="text/html",
                                  body="<!doctype html><html><body>smoke</body></html>")
                else:
                    route.fulfill(status=200, content_type="text/plain", body="")

            page.route("**/*", route_handler)
            page.goto("https://www.kuaishou.com/new-reco",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(300)

            # 1) 钩子装上, 两侧版本号一致
            ver = page.evaluate("window.__DY_HOOK_VER__")
            from douyin_dl.watcher import HOOK_VER
            results.append(("hook version installed", ver == HOOK_VER, ver))
            results.append(("hooked flag",
                            page.evaluate("window.__DY_HOOKED_V2__ === true"), ""))
            # 2) fetch 被替换且页面覆盖不掉(历史上 writable 描述符踩过大坑)
            results.append(("fetch replaced", page.evaluate(
                "window.fetch.toString().indexOf('native code') === -1"), ""))
            results.append(("fetch override blocked", page.evaluate(
                "(()=>{try{window.fetch=function(){};"
                "return window.fetch.toString().indexOf('native code')===-1}"
                "catch(e){return true}})()"), ""))
            # 3) 快手收藏动作: URL 命中 ACT_EP, 请求体原样带过来
            page.evaluate("""() => fetch(
                "https://www.kuaishou.com/rest/v/photo/collect",
                {method:"POST", headers:{"Content-Type":"application/json"},
                 body: JSON.stringify({photoId:"3xb42hegyea3mzc",
                                       userId:"u1", collect:1})})""")
            page.wait_for_timeout(500)
            acts = [r for r in REPORTS if r["kind"] == "action"]
            results.append(("collect action reported",
                            any("/rest/v/photo/collect" in r["url"] for r in acts),
                            [r["url"] for r in acts][:1]))
            results.append(("collect req body carried",
                            any('"collect":1' in r["req"] and
                                "3xb42hegyea3mzc" in r["req"] for r in acts),
                            [r["req"] for r in acts][:1]))
            # 4) 快手数据流: 命中 DATA_EP 并上报 data
            page.evaluate("""() => fetch(
                "https://www.kuaishou.com/rest/v/feed/hot",
                {method:"POST", body:"{}"})""")
            page.wait_for_timeout(500)
            datas = [r for r in REPORTS if r["kind"] == "data"]
            results.append(("feed/hot reported as data",
                            any("/rest/v/feed/hot" in r["url"] for r in datas),
                            [r["url"] for r in datas][:1]))
            # 5) 点击快手收藏按钮: click 事件里必须带 photoId。
            #    这里守住 idFromNode 的 el 被耗尽回归(v1.7 真实踩过)。
            page.evaluate("""() => {
                const a = document.createElement("a");
                a.className = "collect-btn";
                a.href = "/short-video/3xb42hegyea3mzc";
                a.innerText = "收藏";
                document.body.appendChild(a);
            }""")
            page.click("a.collect-btn")
            page.wait_for_timeout(300)
            click_ok = False
            click_bodies = []
            for r in (r for r in REPORTS if r["kind"] == "click"):
                click_bodies.append(r["body"][:120])
                try:
                    if json.loads(r["body"]).get("id") == "3xb42hegyea3mzc":
                        click_ok = True
                except Exception:
                    pass
            results.append(("ks click reports photoId", click_ok, click_bodies[:2]))
            # 6) 抖音回归: 15 位纯数字 id 仍能解析(用 dispatchEvent 免得元素
            #    因为 outerHTML 替换掉 body 而落到不可见状态)
            page.evaluate("""() => {
                const a = document.createElement("a");
                a.setAttribute("data-e2e", "video-player-collect");
                a.href = "/video/7705427599274719717";
                a.style.display = "block";
                document.body.appendChild(a);
                a.dispatchEvent(new MouseEvent("click", {bubbles:true}));
            }""")
            page.wait_for_timeout(300)
            dy_ok = any(
                json.loads(r["body"]).get("id") == "7705427599274719717"
                for r in REPORTS if r["kind"] == "click"
                and "7705427599274719717" in r["body"]
            )
            results.append(("douyin click still resolves id", dy_ok, ""))
        finally:
            br.close()
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    print("=== HOOK_JS 真实注入冒烟测试 ===")
    all_ok = True
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}   {str(detail)[:110]}")
        all_ok = all_ok and ok
    print(f"  TOTAL: {sum(1 for _, o, _ in results if o)}/{len(results)} passed")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
