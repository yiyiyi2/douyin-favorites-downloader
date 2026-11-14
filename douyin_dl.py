#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音 / 快手 / B站收藏自动下载 —— 主程序

用法:
    python douyin_dl.py                 # 启动, 然后去抖音点收藏
    python douyin_dl.py --download-all  # 启动时先把现有收藏补下回来
    python douyin_dl.py --url <链接>     # 只下这一个视频, 不启动监听
    python douyin_dl.py --check         # 检查浏览器登录状态
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
STATE_FILE = WORKSPACE / ".state.json"
PROFILE_DIR = WORKSPACE / ".chrome_profile"
LOG_FILE = WORKSPACE / "douyin_dl.log"
SETTINGS_FILE = WORKSPACE / ".settings.json"
WEB_FILE = WORKSPACE / "douyin_dl" / "web" / "index.html"

def _dep_error(exc: Exception):
    """依赖缺失/损坏时给一句人话并写进日志, 别只甩一个 traceback。

    v1.6: 以前顶层 import 直接失败, pythonw 守护模式下连一行日志都没有,
    用户只看到"程序一直重启", 完全不知道要 pip install。"""
    import traceback
    msg = (f"[{time.strftime('%H:%M:%S')}] 依赖加载失败: {exc}\n"
           f"  请在本目录执行: pip install -r requirements.txt\n"
           f"{traceback.format_exc()}")
    try:
        print(msg)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


try:
    from douyin_dl.ui import (
        EventBus,
        filter_candidates,
        load_settings,
        save_settings,
        start_ui,
    )
    import douyin_dl.ui as ui_mod
except Exception as _e:      # noqa: BLE001
    _dep_error(_e)
    raise

# 下载目录的运行时取值: 设置文件 out_dir > 启动参数 > 工作区/downloads。
# OUT_DIR 在 main() 里按上面顺序初始化一次; 下载线程里每次都经 get_out_dir() 取,
# 这样面板里改了目录, 不重启主进程也能对下一个视频生效。
OUT_DIR = WORKSPACE / "downloads"
# v1.6: 用户的 Chrome 调试端口由 --port 决定, 别再到处写死 9222。
# 写死会导致自定义端口时 cookie 同步读错端口 -> 详情地址 403。
_CDP_PORT = 9222


def _dl_workers() -> int:
    """分段并发下载的连接数(0/1 = 关掉并发, 退回单连接)。

    CDN 一般限的是单条连接的速率, 不是账号速率 —— 实测同一个 B站 448MB
    4K 轨, 单连接 1.96 MB/s, 8 路并发 13.37 MB/s。
    """
    try:
        n = int(load_settings(SETTINGS_FILE).get("dl_workers") or 8)
    except Exception:
        return 8
    return max(0, min(16, n))


def get_out_dir() -> Path:
    try:
        v = str(load_settings(SETTINGS_FILE).get("out_dir") or "").strip()
    except Exception:
        v = ""
    if not v:
        return OUT_DIR
    p = Path(v).expanduser()
    if not p.is_absolute():
        p = (WORKSPACE / p).resolve()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        return OUT_DIR          # 目录建不出来(盘不存在/没权限)就还用旧的
    return p


def save_settings_file(path, data):
    save_settings(path, data)


_log_lock = threading.Lock()

try:
    from douyin_dl.core import (
        build_filename,
        build_session,
        download,
        download_smart,
        is_kuaishou_id,
        parse_any,
        parse_aweme,
        probe_size,
        run_hidden,
        safe_name,
        verify_video,
        VideoInfo,
    )
    from douyin_dl import bili
except Exception as _e:      # noqa: BLE001
    _dep_error(_e)
    raise


def refetch_aweme(aid: str, session) -> dict | None:
    """
    抓一次视频详情的 aweme 数据: 用无头浏览器开视频详情页, 让页面自己发签名请求,
    拦下 /aweme/detail/ 的响应。抓不到返回 None。

    以前每次调用都新起一个 chromium(启动 1~2 秒 + 加载 + 轮询), 单个视频要 45 秒,
    排队根本追不上翻页。现在改成常驻一个无头浏览器 + 一个专属线程, 每次只新开一个
    标签页, 一个视频 2~4 秒。

    v1.7 快手: 直链不过期、不需要 Cookie, 所以快手 id 优先复用 submit() 时
    存下来的那份包装对象(零网络、零风控风险); 只有缓存里没有才走无头兜底。
    """
    if is_kuaishou_id(aid):
        with _ks_last_lock:
            cached = _KS_LAST.get(aid)
        if cached:
            return cached
        # 缓存里没有(通常是进程重启后面板点重试): 抖音那套 _hd_fetch 会拿
        # photoId 去开 douyin.com/video/ 白等 30 秒, 快手详情页又有版本风控,
        # 都不可靠。直接返回 None, 让面板提示"重新收藏一次即可恢复"。
        return None
    with _refetch_lock:
        return _hd_fetch(aid)


# v1.7: 最近提交过的快手包装对象。重试时不再打开详情页。
# 有上限, 长跑不会吃内存; 集合小(几十个), 线性淘汰足够。
_KS_LAST: dict[str, dict] = {}
_KS_LAST_CAP = 200
_ks_last_lock = threading.Lock()
_refetch_lock = threading.Lock()

# 常驻无头浏览器的请求队列: (aid, cookies, box)
_hd_q: "queue.Queue" = queue.Queue()
_hd_lock = threading.Lock()
_hd_born = False
_hd_cookie_cache: tuple[float, list] = (0.0, [])


def _hd_cookies_from_browser() -> list:
    """从用户浏览器的调试端口读 douyin cookie。60 秒内复用, 省掉每次 CDP 往返。"""
    global _hd_cookie_cache
    ts, cached = _hd_cookie_cache
    if cached and (time.time() - ts) < 60:
        return cached
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{_CDP_PORT}/json/version", timeout=3):
            pass
    except Exception:
        return cached or []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
            ctx = br.contexts[0] if br.contexts else None
            if ctx is None:
                br.close()
                return cached or []
            cooked = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                       "path": c.get("path", "/")} for c in ctx.cookies()
                      if "douyin" in (c.get("domain") or "")]
            br.close()
        _hd_cookie_cache = (time.time(), cooked)
        return cooked
    except Exception:
        return cached or []


def _hd_loop():
    """专属线程: 持有常驻无头浏览器, 按请求开标签页抓详情。"""
    from playwright.sync_api import sync_playwright
    pw = None
    br = None
    ctx = None
    while True:
        try:
            aid, cookies, box = _hd_q.get()
        except Exception:
            return
        if aid is None:
            break
        page = None
        try:
            # 浏览器没起来(第一次/上次崩了)就重建
            if ctx is None:
                pw = sync_playwright().start()
                br = pw.chromium.launch(
                    headless=True, channel="chrome",
                    args=["--disable-blink-features=AutomationControlled",
                          "--mute-audio", "--no-first-run", "--disable-gpu",
                          "--disable-extensions", "--disable-dev-shm-usage"])
                ctx = br.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}, locale="zh-CN")
            try:
                ctx.clear_cookies()
                if cookies:
                    ctx.add_cookies(cookies)
            except Exception:
                pass

            result: dict = {}

            def on_resp(r):
                try:
                    if "/aweme/detail/" in r.url and not result:
                        d = r.json()
                        det = (d.get("data") or {}).get("aweme_detail") or d.get("aweme_detail")
                        if isinstance(det, dict) and (det.get("video") or det.get("images")):
                            result.update(det)
                except Exception:
                    pass

            page = ctx.new_page()
            # 只为了拿 detail 接口的 JSON, 不需要画面:
            # 拦掉视频/图片/字体/样式等一切大流量资源。页面照样会发 detail 请求,
            # 但不下载视频本体 —— 省流量, 页面也加载得快得多。
            def _route(route):
                try:
                    u = route.request.url
                    rt = route.request.resource_type
                    if (rt in ("media", "image", "font", "stylesheet") or
                            ".mp4" in u or "douyinvod.com" in u or
                            "douyinpic.com" in u or ".m3u8" in u):
                        return route.abort()
                except Exception:
                    pass
                return route.continue_()
            page.route("**/*", _route)
            page.on("response", on_resp)
            page.goto(f"https://www.douyin.com/video/{aid}",
                      wait_until="domcontentloaded", timeout=20000)
            # detail 响应通常在页面加载后 1~2 秒内就到; 给 6 秒足够
            for _ in range(24):
                if result:
                    break
                page.wait_for_timeout(250)
            box["aweme"] = dict(result) or None
        except Exception as e:  # noqa: BLE001
            box["err"] = e
            # 浏览器可能已经废了, 下次重建
            try:
                if ctx is not None:
                    ctx.close()
            except Exception:
                pass
            try:
                if br is not None:
                    br.close()
            except Exception:
                pass
            ctx = br = None
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass
            box["done"].set()


def _hd_fetch(aid: str) -> dict | None:
    """把抓取请求丢给常驻线程, 等结果。"""
    global _hd_born
    with _hd_lock:
        if not _hd_born:
            threading.Thread(target=_hd_loop, name="dy-detail-browser", daemon=True).start()
            _hd_born = True
    cookies = _hd_cookies_from_browser()
    if not cookies:
        return None          # 调试端口没开/没 cookie, 直接失败别浪费时间
    box = {"done": threading.Event(), "aweme": None, "err": None}
    _hd_q.put((aid, cookies, box))
    if not box["done"].wait(timeout=30):
        return None
    if box.get("err") is not None:
        return None
    return box.get("aweme")


def log(msg: str = ""):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass   # pythonw 下没有 stdout, print 会抛错; 守护模式全是 pythonw
        try:
            # 日志只增不减, 跑几个月能把磁盘吃掉。超过 5MB 就滚一份 .1(只留一代)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
                bak = LOG_FILE.with_suffix(".log.1")
                try:
                    if bak.exists():
                        bak.unlink()
                    LOG_FILE.rename(bak)
                except Exception:
                    pass
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 状态: 记录已下载的 aweme_id, 避免重复下载
# ---------------------------------------------------------------------------

class State:
    def __init__(self, path: Path):
        self.path = path
        self.done: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.load()

    def load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                # v1.6: 顶层必须是 dict。文件写坏成 [] / null / "abc" 时,
                # 旧版直接赋给 self.done, 之后 has()/mark() 在 list 上 .pop /
                # [key]= 要么 TypeError 要么悄悄写进错误结构, 全盘统计错乱。
                self.done = data if isinstance(data, dict) else {}
            except Exception:
                self.done = {}

    def has(self, aweme_id: str) -> bool:
        with self.lock:
            return aweme_id in self.done

    def remove(self, aweme_id: str) -> bool:
        """删一条记录并落盘。面板「删除」走这里, 和 mark() 共用一把锁, 不会互相覆盖。"""
        with self.lock:
            if str(aweme_id) not in self.done:
                return False
            self.done.pop(str(aweme_id), None)
            try:
                self.path.write_text(
                    json.dumps(self.done, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            except Exception:
                pass
            return True

    def mark(self, aweme_id: str, path: Path, quality: str):
        with self.lock:
            self.done[aweme_id] = {
                "file": str(path.relative_to(WORKSPACE)) if path.is_relative_to(WORKSPACE) else str(path),
                "quality": quality,
                "time": int(time.time()),
            }
            try:
                self.path.write_text(
                    json.dumps(self.done, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 下载器线程
# ---------------------------------------------------------------------------

class Downloader(threading.Thread):
    def __init__(self, state: State, session, log_fn=log, bus=None):
        super().__init__(daemon=True)
        self.q: queue.Queue[tuple[dict, str]] = queue.Queue()
        self.state = state
        self.session = session
        self.log = log_fn
        self.bus = bus
        self.count_ok = 0
        self.count_skip = 0
        self.count_fail = 0
        self._rec_prefetch: dict[str, int] = {}   # 推荐页详情预取的重试计数
        # v2.1.1: B站单独一个 session。B站 playurl 按登录态给画质
        # (匿名 480P / 登录 1080P / 大会员 4K), 所以必须带用户浏览器里的
        # B站 Cookie。但**不能**塞进 self.session —— 那是抖音的会话,
        # 里面已经有抖音 Cookie; 混在一起会把抖音 sessionid 发给
        # api.bilibili.com, B站的 SESSDATA 也会被发给抖音 CDN。
        # 两条通道各用各的 session, Cookie 互不可见。
        self.bili_session = build_session()
        self.bili_session.headers.update({
            "Referer": bili.REFERER_BILI,
            "Origin": "https://www.bilibili.com",
        })

    # -- 面板事件小工具 ------------------------------------------------
    def _ev(self, name, *a):
        if not self.bus:
            return
        try:
            getattr(self.bus, name)(*a)
        except Exception:
            pass

    def submit(self, aweme: dict, source: str):
        self.q.put((aweme, source))
        aid = str(aweme.get("aweme_id") or "")
        # v1.7: 快手直链不过期, 把包装对象留一份给「重试」用。
        # 重试时不需要再开无头详情页(会被快手风控挡), 直接复用这份即可。
        if aid and is_kuaishou_id(aid):
            with _ks_last_lock:
                _KS_LAST[aid] = aweme
                while len(_KS_LAST) > _KS_LAST_CAP:
                    _KS_LAST.pop(next(iter(_KS_LAST)), None)
        if self.bus:
            if aid:
                try:
                    _i = parse_any(aweme)
                    self._ev("add_waiting", aid, _i.desc[:60], _i.author, source)
                except Exception:
                    self._ev("add_waiting", aid, "", "", source)

    def run(self):
        while True:
            aweme, source = self.q.get()
            try:
                self.process(aweme, source)
            except Exception as e:  # noqa: BLE001
                self.log(f"  ! 处理出错: {e}")
                self.count_fail += 1
            finally:
                self.q.task_done()

    def delete_record(self, aid: str) -> bool:
        """面板删除一条记录: 只负责 .state.json, 文件由面板那边删。"""
        try:
            return self.state.remove(str(aid))
        except Exception:
            return False

    def retry(self, aid: str) -> tuple[bool, str]:
        """面板点重试: 用无头浏览器重新抓一次详情, 抓到就重新入队。

        v1.6.2: 返回值从 bool 改成 (ok, 原因)。面板要能告诉用户"这次为什么还是
        不行"(拿不到详情 / 视频可能已删除), 否则点完只看到失败项还在, 像是没反应。
        """
        try:
            aw = refetch_aweme(aid, self.session)
        except Exception as e:
            self.log(f"  ! 重试 {aid} 抓取失败: {e}")
            return False, f"抓取异常: {str(e)[:60]}"
        if not aw:
            self.log(f"  ! 重试 {aid}: 拿不到视频信息")
            if is_kuaishou_id(aid):
                return False, "快手视频信息已失效, 往下滑一下让列表重新加载再收藏一次即可"
            return False, "拿不到视频详情(可能已删除/私密/需要重新登录)"
        # v1.6: 走 submit() 而不是直接 q.put(), 让面板收到 add_waiting,
        # 用户能在队列里看到这条正在重试, 而不是"点了没动静"。
        self.submit(aw, "重试")
        return True, ""

    def _fill_bili(self, info: VideoInfo) -> VideoInfo:
        """
        给 B 站视频补齐画质信息: view(拿 cid + 标题/作者) → playurl(DASH 轨)。

        收藏列表/推荐流给的信息很浅(可能只有 bvid), 而 DASH 码率表必须
        另外请求 playurl 才有, 且需要 cid。这两步都是普通 REST, 不需要
        浏览器签名, 所以直接 requests 拿。失败就把原 info 还回去,
        让上层按"没有可用地址"处理并给出明确原因。
        """
        bvid = info.aweme_id
        try:
            # v2.1.1: 用 bili_session(自带用户浏览器里的 B站登录 Cookie)。
            # 用抖音的 self.session 时 playurl 走匿名态, 最高只给 480P;
            # 带上 SESSDATA(大会员) 才会返回 1080P+/4K/8K 那些高画质轨。
            view = bili.fetch_view(self.bili_session, bvid, log=self.log)
            if not view:
                self.log("  ! B站 view 拿不到(可能已删除/私密/需要登录)")
                return info
            # view 里有完整标题/作者/封面, 比列表接口的准
            fresh = bili.parse_bili_view(view, bvid)
            if fresh.desc:
                info.desc = fresh.desc
            if fresh.author:
                info.author = fresh.author
            if fresh.author_id:
                info.author_id = fresh.author_id
            if fresh.cover_urls:
                info.cover_urls = fresh.cover_urls
            if fresh.duration:
                info.duration = fresh.duration
            if fresh.create_time:
                info.create_time = fresh.create_time
            info.raw = fresh.raw
            cid = int((fresh.raw or {}).get("cid") or 0)
            if not cid:
                self.log("  ! B站 view 里没有 cid, 跳过")
                return info
            dash = bili.fetch_dash(self.bili_session, bvid, cid, log=self.log)
            if not dash:
                self.log("  ! B站 playurl 拿不到播放地址")
                return info
            # 老视频只有 durl(音视频合一的 mp4 分段), 不需要合并
            durl_cands = bili.dash_to_durl_candidates(dash)
            if durl_cands:
                info.candidates = durl_cands
                self.log(f"  · B站老格式(durl): {len(durl_cands)} 段, 无需合并")
                return info
            bili.attach_dash(info, dash)
            if info.candidates:
                qs = sorted({c.gear_name for c in info.candidates}, key=len)
                self.log(f"  · B站画质档: {'/'.join(qs)} "
                         f"(共 {len(info.candidates)} 条轨, 音视频分离需合并)")
            return info
        except Exception as e:  # noqa: BLE001
            self.log(f"  ! B站补全信息异常: {str(e)[:100]}")
            return info

    def process(self, aweme: dict, source: str):
        # 推荐页(feed/related)列表里的候选 URL 带 ply_type=4&tk=webid,
        # 是页面播放器的内部令牌流, requests 直接下会 403。
        # 必须先走无头浏览器抓一次视频详情页, 换成 detail 接口的可直下 URL。
        # 抓不到就重新排队等下一轮(最多 3 次), 绝不用列表自带地址硬试 ——
        # 那些地址带 ply_type=4&tk=webid, 直下必 403, 纯浪费带宽和重试次数。
        if source == "推荐页":
            aid0 = str(aweme.get("aweme_id") or "")
            tried = self._rec_prefetch.get(aid0, 0)
            detail = None
            try:
                detail = refetch_aweme(aid0, self.session)
            except Exception as e:  # noqa: BLE001
                self.log(f"  · 推荐页预取详情异常({e})")
            if detail:
                self._rec_prefetch.pop(aid0, None)
                aweme = detail
                self.log("  · 已换成详情页地址(可直下)")
            else:
                if tried < 2:
                    # 没抓到: 等 8 秒重新排队, 给下一个视频让出抓取通道
                    self._rec_prefetch[aid0] = tried + 1
                    self.log(f"  · 详情抓取没就绪, 8 秒后重新排队({tried + 1}/3)")
                    self._ev("pop_waiting", aid0)
                    _th2 = threading.Timer(
                        8.0, lambda: self.q.put((aweme, "推荐页")))
                    _th2.daemon = True
                    _th2.start()
                    return
                self._rec_prefetch.pop(aid0, None)
                self.log("  ! 重试 3 次都没抓到详情, 放弃这个推荐视频(不浪费 403 重试)")
                self._ev("pop_waiting", aid0)
                self._ev("fail", aid0, "详情页抓取失败(推荐页)")
                self.count_fail += 1
                return

        # v1.7: 快手包装对象走 parse_kuaishou, 抖音原生对象走 parse_aweme。
        # 两者都产 VideoInfo, 下面所有画质/文件名/落盘逻辑无需分支。
        info = parse_any(aweme)
        if not info.aweme_id:
            return

        # v2.1 B站: 列表接口里没有画质数据, 必须补 view(拿 cid) + playurl(拿 DASH)。
        # 这两个接口直连即可, 不需要无头浏览器, 所以不用像抖音那样排队等详情。
        if info.platform == "bilibili" and not info.candidates:
            info = self._fill_bili(info)

        if self.state.has(info.aweme_id):
            self.count_skip += 1
            self._ev("pop_waiting", info.aweme_id)
            self._ev("skip")
            return

        label = f"{info.author} - {info.desc[:40]}" if info.author else info.desc[:50]
        self.log("")
        self.log(f"▼ [{source}] {label or info.aweme_id}")

        if info.is_image_post and not info.candidates:
            self._save_images(info)
            return

        # 界面里可以限制最高画质(省流量); 没设就是最高画质
        pref = load_settings(SETTINGS_FILE).get("quality", "max")
        cands = filter_candidates(info.candidates, pref)

        # 按画质从高到低排序。首选失败(常见的 403: 有的地址带 tk=webid,
        # 需要播放器内部令牌, 直接下载会被拒)就依次退到下一个候选。
        order = sorted(cands, key=lambda c: c.score(), reverse=True)
        best = order[0] if order else None
        if not best:
            self.log("  ! 没有可用的视频地址, 跳过")
            self.count_fail += 1
            self._ev("pop_waiting", info.aweme_id)
            self._ev("finish", info.aweme_id, False)
            self._ev("fail", info.aweme_id, "没有可用的视频地址",
                     info.desc[:50], info.author,
                     (info.cover_urls[0] if info.cover_urls else ""))
            return

        # 探一下体积, 顺便验证地址可用
        size = probe_size(self.session, best.url)
        res = f"{best.width}x{best.height}" if best.width else "?"
        cands = len(info.candidates)
        self.log(
            f"  画质: {res}  码率={best.bitrate or '?'}  "
            f"候选={cands} 个  {size / 1048576:.1f} MB" if size else
            f"  画质: {res}  候选={cands} 个  大小未知"
        )

        stem = build_filename(info)
        dest_dir = get_out_dir()
        # 面板里开了「按作者分文件夹」就归类: 下载目录/作者名/xxx.mp4
        # v1.6: 必须基于 get_out_dir() 而非旧全局 OUT_DIR, 否则面板里改了下载目录
        # 后, 普通视频写新目录、按作者归类的却还写旧目录, 用户找不到文件。
        if info.author and load_settings(SETTINGS_FILE).get("by_author"):
            safe = "".join(ch for ch in info.author if ch not in '\\/:*?"<>|').strip() or "未知作者"
            dest_dir = get_out_dir() / safe[:40]
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
            except Exception as _e:
                self.log(f"  ! 作者目录建不出来({_e}), 直接用下载根目录")
                dest_dir = get_out_dir()
        dest = dest_dir / f"{stem}.mp4"

        # 已经有同名文件(可能是上次没记进 state)就直接登记。
        # v1.6: 但得先确认它真是个视频 —— 旧版只看 size>0, 一个上次下载
        # 中断留下的半截 .part 改名、或 403 存下来的 HTML 错误页, 会被当成
        # "已完成"永久登记, 以后再也不会重下, 用户永远缺这一条。
        if dest.exists() and dest.stat().st_size > 0:
            if not verify_video(dest):
                self.log(f"  ! 已存在的 {dest.name} 不是有效视频, 删掉重下")
                try:
                    dest.unlink()
                except Exception:
                    pass
            else:
                self.log(f"  已存在, 跳过: {dest.name}")
                self.state.mark(info.aweme_id, dest, f"{res}")
                self.count_skip += 1
                self._ev("pop_waiting", info.aweme_id)
                self._ev("skip")
                return

        self.log(f"  下载中 -> {dest_dir.name}/{dest.name}")
        self._ev("begin", info.aweme_id, {
            "title": info.desc[:70] or info.aweme_id,
            "author": info.author,
            "cover": (info.cover_urls[0] if info.cover_urls else ""),
            "quality": res,
            "size": size,
        })
        t0 = time.time()
        ok = False
        aid = info.aweme_id
        for i, cand in enumerate(order[:5]):
            if i:
                self.log(f"  换候选 {i + 1}/{min(len(order), 5)}: "
                         f"{cand.width}x{cand.height} 码率={cand.bitrate or '?'}")
            # v2.1 B站: DASH 音视频分离, 要走「下两路 + ffmpeg 合并」;
            # 抖音/快手是音视频合一的 mp4, 走原来的单流下载。
            if getattr(cand, "needs_merge", False):
                got = bili.download_dash(
                    self.bili_session, cand, dest, log=self.log,
                    on_progress=lambda d, t: self._ev("progress", aid, d, t))
            else:
                # v2.2: 大文件走分段并发(CDN 限的是单连接速率)
                got = download_smart(
                    self.session, cand.url, dest, log=self.log,
                    on_progress=lambda d, t: self._ev("progress", aid, d, t),
                    workers=_dl_workers())
            if got:
                ok = True
                best = cand
                # v1.6: 退到备用候选后, res 还停在 order[0] 的画质, 结果
                # state.json 和 _meta 把"实际下到的 720p"记成"首选的 1080p",
                # 以后重看/统计全错。用真正下成功的这个候选重算一次。
                res = f"{best.width}x{best.height}" if best.width else res
                break
        if not ok:
            self.log("  × 所有候选地址都下载失败")
            self.count_fail += 1
            self._ev("finish", info.aweme_id, False)
            self._ev("fail", info.aweme_id, "所有候选地址都下载失败",
                     info.desc[:50], info.author,
                     (info.cover_urls[0] if info.cover_urls else ""))
            return

        if not verify_video(dest):
            # 不能留: 下次再下同一视频会命中下面的「已存在, 跳过」, 于是永远是个坏文件
            self.log("  × 文件校验失败(可能不是有效视频), 已删掉")
            try:
                dest.unlink()
            except Exception:
                pass
            self.count_fail += 1
            self._ev("finish", info.aweme_id, False)
            self._ev("fail", info.aweme_id, "文件校验失败",
                     info.desc[:50], info.author,
                     (info.cover_urls[0] if info.cover_urls else ""))
            return

        dt = time.time() - t0
        mb = dest.stat().st_size / 1048576
        speed = mb / dt if dt > 0 else 0
        self.log(f"  ✔ 完成  {mb:.1f} MB  {dt:.1f}s  ({speed:.1f} MB/s)")

        # 顺手存一份元数据, 方便以后查
        self._save_meta(info, dest, res)
        self.state.mark(info.aweme_id, dest, res)
        self.count_ok += 1
        self._ev("set_speed", speed)
        self._ev("finish", info.aweme_id, True, {"size": dest.stat().st_size})

    def _save_meta(self, info: VideoInfo, dest: Path, quality: str):
        meta = {
            "aweme_id": info.aweme_id,
            "desc": info.desc,
            "author": info.author,
            "author_id": info.author_id,
            "create_time": info.create_time,
            "duration_ms": info.duration,
            "music": f"{info.music_title} - {info.music_author}".strip(" -"),
            "file": dest.name,
            "quality": quality,
            "cover": (info.cover_urls[0] if info.cover_urls else ""),
            "all_candidates": [
                {"w": c.width, "h": c.height, "bitrate": c.bitrate, "gear": c.gear_name}
                for c in info.candidates
            ],
            "downloaded_at": int(time.time()),
        }
        # v1.6: 目录创建和写入一起进 try。旧版 mkdir 在 try 外, 目录建不出来
        # (盘拔了/权限变了)会抛异常打断整个 process(), 明明视频已经下好了却
        # 走不到 state.mark(), 下次启动又把它当下过一遍 —— 白下还占空间。
        try:
            meta_dir = get_out_dir() / "_meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / f"{info.aweme_id}.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _save_images(self, info: VideoInfo):
        """图文作品: 逐张下原图。"""
        folder = get_out_dir() / build_filename(info)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as _e:
            self.log(f"  ! 图文目录建不出来({_e}), 跳过这条图文")
            self.count_fail += 1
            self._ev("pop_waiting", info.aweme_id)
            self._ev("finish", info.aweme_id, False)
            return
        ok = 0
        for i, u in enumerate(info.image_urls, 1):
            ext = ".jpg"
            if ".png" in u.lower():
                ext = ".png"
            elif ".webp" in u.lower():
                ext = ".webp"
            p = folder / f"{i:02d}{ext}"
            if download(self.session, u, p, log=self.log):
                ok += 1
        self.log(f"  ✔ 图文作品: 已保存 {ok}/{len(info.image_urls)} 张 -> {folder.name}/")
        self._ev("pop_waiting", info.aweme_id)   # 图集不走 begin, 得自己把排队项摘掉
        if ok:
            self.state.mark(info.aweme_id, folder, "images")
            self.count_ok += 1
            self._ev("finish", info.aweme_id, True)
        else:
            self.count_fail += 1
            self._ev("finish", info.aweme_id, False)


# ---------------------------------------------------------------------------
# 单链接下载(不依赖浏览器)
# ---------------------------------------------------------------------------

def download_single(url: str, state: State) -> int:
    """直接下载一个链接: 用 yt-dlp 抓详情(它内置了抖音解析)。"""
    import subprocess

    log(f"解析链接: {url}")
    dest_dir = get_out_dir()          # v1.6: 跟随设置里的下载目录, 别用旧全局 OUT_DIR
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception as _e:
        log(f"  ! 输出目录 {dest_dir} 建不出来({_e}), 下载无法继续")
        return 1
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(dest_dir / "%(uploader)s_%(title).70B_%(id)s.%(ext)s"),
        "--print", "after_move:filepath",
        "--no-warnings",
        url,
    ]
    try:
        r = run_hidden(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    except FileNotFoundError:
        log("  ! 找不到 yt-dlp, 请先执行: pip install -r requirements.txt")
        return 1
    except subprocess.TimeoutExpired:
        log("  ! yt-dlp 超过 5 分钟没结束, 已中止")
        return 1
    if r.returncode != 0:
        err_lines = [x for x in (r.stderr or "").strip().splitlines() if x.strip()]
        log("  ! yt-dlp 解析失败:")
        log("    " + (err_lines[-1] if err_lines else f"退出码 {r.returncode}, 无错误输出"))
        # v1.6: 旧提示指向根本不存在的 --watch 模式, 改成人话。
        log("    该链接可能需要登录态/已失效; 普通收藏请直接双击 启动收藏下载.bat 后在抖音点收藏")
        return 1
    for line in (r.stdout or "").splitlines():
        if line.strip():
            log(f"  ✔ 已保存: {line.strip()}")
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _focus_existing_window(port: int = 9222) -> bool:
    """
    第二次双击启动时, 把已经在跑的那个浏览器窗口(抖音 / 快手)提到最前面。

    不然用户只会看到"已经在运行了"然后什么都没有, 很像程序坏了。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=5000)
            pages = [pg for ctx in browser.contexts for pg in ctx.pages
                     if not pg.is_closed()]
            # v1.7: 两个站点都算"我们的窗口", 抖音优先排前面
            pages.sort(key=lambda pg: 0 if any(
                d in (pg.url or "") for d in ("douyin.com", "kuaishou.com")) else 1)
            for pg in pages:
                try:
                    pg.bring_to_front()
                    return True
                except Exception:
                    continue
    except Exception:
        return False
    return False


def _acquire_instance_lock():
    """
    保证同一时间只有一个监听进程。

    踩过的坑: 重复启动会起来两个进程, 都挂在同一个浏览器上, 互相抢页面、
    同一条收藏被下载两遍, 而且关掉其中一个浏览器就整个不能用了。
    用 Windows 文件锁实现, 进程被强杀时锁也会自动释放。
    """
    import os

    try:
        import msvcrt
    except ImportError:
        return object()  # 非 Windows 直接放行

    try:
        fd = os.open(str(WORKSPACE / ".watcher.lock"), os.O_RDWR | os.O_CREAT)
    except OSError:
        return object()  # 拿不到锁文件也不该阻止正常使用

    def _close(f):
        try:
            os.close(f)
        except OSError:
            pass

    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        # 锁被占: 先确认是不是真的有活进程, 别让残留锁挡住正常启动。
        # (文件锁理论上随进程消失, 但进程被强杀时偶尔会留下死锁文件)
        if not _has_live_watcher():
            try:
                _close(fd)
                os.remove(str(WORKSPACE / ".watcher.lock"))
                fd = os.open(str(WORKSPACE / ".watcher.lock"), os.O_RDWR | os.O_CREAT)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return fd
            except OSError:
                pass
        _close(fd)
        return None
    return fd


def _has_live_watcher() -> bool:
    """有没有真的 douyin_dl.py 进程在跑(按命令行匹配, 不看锁文件)。

    注意 daemon 守护模式下下载进程跑在 pythonw.exe 里, 只查 python.exe 会漏,
    然后误判"没有活进程"把残留锁删掉, 造成两个守护各拉起一个监听互相抢。

    v1.6: wmic 在 Win11 24H2 起已被系统移除, 一旦 wmic 不存在, 旧版 except
    返回 False -> 每次都以为没有活进程 -> 把别人的活锁删掉 -> 双实例互抢。
    改用 PowerShell Get-CimInstance; 更关键的是**探测失败时保守返回 True**
    (宁可这次不启动、提示用户, 也不能删掉一个可能还活着的锁)。"""
    try:
        import subprocess
        ps = (
            "Get-CimInstance Win32_Process -Filter "
            "\"name='python.exe' or name='pythonw.exe'\" | "
            "Select-Object -ExpandProperty CommandLine"
        )
        out = run_hidden(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-Command", ps],
            capture_output=True, text=True, timeout=12,
        ).stdout.lower()
        return "douyin_dl.py" in out or "daemon.py" in out
    except Exception:
        # 探测失败(超时/PowerShell 被策略拦): 保守认为"可能有活进程",
        # 绝不删锁。最坏是这次启动被挡一下, 好过双实例互相抢同一浏览器。
        return True


_LOCK = None


def main():
    global _LOCK

    ap = argparse.ArgumentParser(
        description="抖音 / 快手 / B站收藏自动下载(最高画质 / 无水印)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", help="只下载指定链接, 不启动监听")
    ap.add_argument("--download-all", action="store_true",
                    help="启动时先把当前收藏里的所有视频补下回来")
    ap.add_argument("--check", action="store_true", help="只检查登录状态")
    ap.add_argument("--port", type=int, default=9222, help="Chrome 调试端口")
    ap.add_argument("--headless", action="store_true", help="无头模式(需要已登录过)")
    ap.add_argument("--output", help="输出目录 (默认 ./downloads)")
    ap.add_argument("--ui-port", type=int, default=8899, help="本地面板端口 (默认 8899)")
    ap.add_argument("--no-ui", action="store_true", help="不启动本地面板, 也不打开浏览器")
    ap.add_argument("--no-open", action="store_true", help="启动面板但不要自动打开浏览器")
    ap.add_argument("--offline", "--player-only", dest="offline",
                    action="store_true",
                    help="纯离线模式: 只起面板+离线播放器, 不碰浏览器/不下载。"
                         "没网也能刷已经下好的视频")
    args = ap.parse_args()

    global OUT_DIR, _CDP_PORT
    # v1.6: 用户可能用 --port 指定非 9222 的调试端口。常驻无头浏览器读 cookie、
    # 切窗口都走模块级 _CDP_PORT, 这里必须同步, 否则自定义端口时详情地址 403。
    _CDP_PORT = args.port
    # 启动时: 命令行 --output 优先, 其次设置文件里的 out_dir, 最后工作区/downloads
    settings0 = load_settings(SETTINGS_FILE)
    if args.output:
        OUT_DIR = Path(args.output).resolve()
        settings0["out_dir"] = str(OUT_DIR)          # 命令行指定时写回设置, 面板能看到
        save_settings_file(SETTINGS_FILE, settings0)
    elif settings0.get("out_dir"):
        OUT_DIR = Path(str(settings0["out_dir"])).expanduser()
        if not OUT_DIR.is_absolute():
            OUT_DIR = (WORKSPACE / OUT_DIR).resolve()
    # v1.6: 目录建不出来(盘拔了/网络盘断了/没权限)不能让整个程序启动就崩,
    # 更不能把一个已经写回设置的坏 out_dir 留着反复坑用户。回落默认目录并提示。
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as _e:
        log(f"  ! 输出目录 {OUT_DIR} 建不出来({_e}), 回落到 {WORKSPACE / 'downloads'}")
        OUT_DIR = WORKSPACE / "downloads"
        try:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    state = State(STATE_FILE)
    session = build_session()

    log("=" * 62)
    log("  抖音 / 快手 / B站收藏自动下载  |  最高画质 · 无水印")
    log("=" * 62)
    log(f"  工作区: {WORKSPACE}")
    log(f"  输出到: {OUT_DIR}")
    log(f"  已记录: {len(state.done)} 个视频")
    log("")

    if args.url:
        return download_single(args.url, state)

    # 延迟导入, 避免 --url 模式也要加载 playwright
    from douyin_dl.watcher import BrowserWatcher

    # v1.6: --check 是纯检查, 必须放在所有副作用之前 —— 旧版它在抢锁、
    # 开面板、起下载线程之后, 结果"只是查个登录状态"却把 8899 面板弹了出来、
    # 还占了实例锁, 检查完也不干净。现在只起一次浏览器查 cookie 就退出。
    if args.check:
        _wc = BrowserWatcher(profile_dir=PROFILE_DIR, port=args.port, log=log,
                             headless=args.headless)
        if not _wc.start_chrome():
            return 2
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
                ctx = b.contexts[0] if b.contexts else b.new_context()
                ok = BrowserWatcher.check_login(ctx)
                # 注意: 这是用户真实在用的 Chrome(带所有标签页), 检查完
                # 只靠 sync_playwright 上下文退出时断开连接即可, 绝不 b.close()
                # 去动用户的浏览器。
        except Exception as _e:
            log(f"  ! 检查登录出错: {_e}")
            return 2
        log(f"  登录状态: {'✔ 已登录' if ok else '✘ 未登录'}")
        return 0 if ok else 1

    # v2.5: 纯离线模式 —— 只读本地已下载的视频, 完全不联网。
    # 旧版播放器寄生在下载主程序里, 而主程序启动链路必然 start_chrome()
    # 连抖音: 没网 / 没开 Chrome 时就起不来, 于是离线刷视频根本用不了。
    # 这里在**抢实例锁之前**返回, 这样用户可以在下载进程照常跑着的同时
    # 再开一个离线播放器(只读, 不写 .state.json, 不冲突)。
    if args.offline:
        bus = EventBus(WORKSPACE, OUT_DIR, STATE_FILE, LOG_FILE)
        bus.set_status(running=False, browser="", note="离线模式(不联网)")
        # 8899 上已经有别的面板(比如下载主程序在跑)时, start_ui 只会挂一个
        # 看门狗等接管, 不会再开一份 —— 这时直接把已有的那个给用户用就行。
        _already = False
        try:
            import urllib.request as _ur
            with _ur.urlopen(f"http://127.0.0.1:{args.ui_port}/api/state",
                             timeout=2) as _r:
                _already = _r.status == 200
        except Exception:
            pass
        ui_url = start_ui(bus, WEB_FILE, port=args.ui_port,
                          open_browser=not args.no_open, log=log)
        log(f"  面板: {ui_url}")
        if _already:
            log("  8899 上已经有一个面板在跑, 直接用那个即可")
        log(f"  离线播放器: {ui_url.rstrip('/')}/player")
        log(f"  视频目录: {OUT_DIR}")
        log("")
        log("  离线模式已就绪 —— 完全不联网, 只播本机已下载的视频。")
        log("  关掉这个窗口/Ctrl+C 即可退出。")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            log("")
            log("  已退出离线模式。")
        return 0

    # 锁必须放在开面板/弹浏览器之前: 不然双击两次会把面板也起两份
    # (旧顺序里第二次启动已经把面板/浏览器弹出来了才在这里退出)。
    _LOCK = _acquire_instance_lock()
    if _LOCK is None:
        log("  ! 已经有一个下载进程在运行了, 这次不重复启动")
        if _focus_existing_window(port=args.port):
            log("    已经把抖音窗口切到前面了, 直接刷就行")
        else:
            log("    没找到正在运行的抖音窗口: 请把原来那个黑色窗口关掉, 再双击启动一次")
        return 3

    bus = EventBus(WORKSPACE, OUT_DIR, STATE_FILE, LOG_FILE)
    ui_url = ""
    if not args.no_ui:
        settings = load_settings(SETTINGS_FILE)
        want_open = bool(settings.get("open_browser", True)) and not args.no_open
        ui_url = start_ui(bus, WEB_FILE, port=args.ui_port,
                          open_browser=want_open, log=log)
        if ui_url:
            bus.set_status(running=False, browser="", note="启动中")
            log(f"  面板: {ui_url}")
            log("")

    dl = Downloader(state, session, bus=bus)
    dl.start()
    bus.on_retry = dl.retry          # 面板「重试」按钮 -> 重新抓详情再入队
    bus.on_delete = dl.delete_record  # 面板「删除」按钮 -> 带锁从 .state.json 摘掉

    watcher = BrowserWatcher(
        profile_dir=PROFILE_DIR,
        port=args.port,
        on_aweme=lambda aw, src: dl.submit(aw, src),
        # 抖音视频地址带 tk=webid, 绑定登录态, 下载必须带同一套 Cookie, 否则 403
        on_cookie=lambda c: session.headers.update({"Cookie": c}),
        log=log,
        headless=args.headless,
        on_status=lambda kw: bus.set_status(**kw),
        # 识别到收藏动作就立刻让面板出现"解析中", 不等详情抓回来
        on_pending=lambda aid: bus.add_waiting(aid, source="解析中"),
        # v2.1.1: B站 Cookie 单独一条通道 -> 只写进 dl.bili_session。
        # B站 playurl 按登录态给画质(匿名 480P / 登录 1080P / 大会员 4K),
        # 必须带上用户的 SESSDATA。但绝不能写进上面那个抖音的 session,
        # 否则两家 Cookie 会互相泄露给对方的服务器。
        on_cookie_bili=lambda c: dl.bili_session.headers.update({"Cookie": c}),
    )
    def _on_fail(aid, why):
        # v1.6.3: 失败时还得把队列里那条「解析中」占位摘掉。v1.6.2 只记了失败
        # 列表, 占位就永远挂在队列上写着"解析中", 用户看到一条卡死的假进度。
        try:
            bus.pop_waiting(aid)
        except Exception:
            pass
        bus.fail(aid, why)

    watcher.on_fail = _on_fail   # 抓不到详情也能在面板失败列表里看到

    if args.download_all:
        log("  模式: 先补下现有收藏, 再进入监听")
        watcher.download_all_on_start = True
    else:
        watcher.download_all_on_start = False

    # v1.4: 自动下推荐页(设置里开关, 默认关)。启动时读设置, 面板改设置时实时切换。
    _conf0 = load_settings(SETTINGS_FILE)
    # v1.6: 用 ui 的类型归一化, 别用裸 bool() —— bool("false") 是 True,
    # 设置文件里一旦被写进字符串 "false"/"0"(老版本/手动改过), 开关就反着来。
    _as_bool = getattr(ui_mod, "_as_bool", lambda v, d=False: bool(v))
    watcher.auto_recommend = _as_bool(_conf0.get("auto_rec", False), False)
    # v1.5: 自动翻页间隔(秒), 设置里可调, 默认 3 秒
    _clamp_gap = getattr(ui_mod, "_clamp_gap", None)
    try:
        watcher.auto_advance_gap = float(_conf0.get("auto_gap") or 3.0)
    except Exception:
        watcher.auto_advance_gap = 3.0
    if _clamp_gap:
        watcher.auto_advance_gap = _clamp_gap(watcher.auto_advance_gap)
    if watcher.auto_recommend:
        log(f"  模式: 自动下载推荐页视频已开启(每 {watcher.auto_advance_gap:g} 秒自动翻一条, 设置里可随时关)")
    bus.on_auto_rec = lambda v: setattr(watcher, "auto_recommend", _as_bool(v, False))
    bus.on_auto_gap = lambda g: setattr(watcher, "auto_advance_gap", _clamp_gap(g) if _clamp_gap else float(g))
    watcher.auto_advance = _as_bool(_conf0.get("auto_adv", True), True)
    bus.on_auto_adv = lambda v: setattr(watcher, "auto_advance", _as_bool(v, True))
    # 翻页要有背压: 队列堵了就先别往下滑, 否则积压会无限长
    watcher.queue_probe = lambda: dl.q.qsize()
    # v1.6: 面板里改下载目录后, ui.py 发 on_out_dir(Path) 回调, 这里同步全局
    # OUT_DIR, 让日志统计、_focus、按作者归类的兜底都跟上新目录(下载线程本身
    # 已经每次 get_out_dir(), 但 main 里的 OUT_DIR 还停在启动时那个旧值)。
    def _on_out_dir(new_dir):
        global OUT_DIR
        try:
            p = Path(new_dir)
            OUT_DIR = p
            log(f"  下载目录已切换到: {p}")
        except Exception:
            pass
    bus.on_out_dir = _on_out_dir

    # v2.1: 面板「打开某个平台」—— 在用户正在用的那个浏览器里新开标签页。
    # 交给 watcher 做(它持有 Playwright 的 greenlet), 比面板自己连 CDP 稳;
    # 面板那边的 _open_site 只在 watcher 还没就绪时兜底。
    def _on_open_site(which: str, url: str) -> bool:
        fn = getattr(watcher, "open_site_tab", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(which, url))
        except Exception as e:      # noqa: BLE001
            log(f"  ! 打开 {which} 失败: {str(e)[:80]}")
            return False

    bus.on_open_site = _on_open_site

    rc = 0
    try:
        rc = watcher.run()
        if rc == 7:
            # 用户主动关闭(自动重开=关 且抖音全部关闭): 让 daemon 知道别再拉起
            log("  已按设置停止监听。下次双击 启动收藏下载.bat 再开始。")
    except KeyboardInterrupt:
        pass
    finally:
        log("")
        log(f"  本次统计: 成功 {dl.count_ok} · 跳过 {dl.count_skip} · 失败 {dl.count_fail}")
        log(f"  文件在: {OUT_DIR}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
