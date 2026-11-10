#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器侧 —— 接管一个已登录的 Chrome, 监听抖音的「收藏」动作。

工作方式
--------
1. 启动(或连上)一个带远程调试端口的 Chrome, 用独立 profile 保存登录态
2. 注入钩子劫持 fetch / XMLHttpRequest
3. 页面自己发出的请求签名永远是正确的 —— 我们只是搭便车读取响应:
     · 数据源: 详情页 / 收藏列表 / 信息流 / 用户作品, 从中缓存 aweme 对象
     · 触发点: 收藏接口被调用时, 取出对应 aweme 丢给下载器
4. 全程不伪造任何请求, 因此不存在 a_bogus 签名和风控问题

为什么不用轮询: 轮询需要自己构造 a_bogus 签名, 极易触发风控且随时失效。
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.request
from urllib.parse import parse_qsl
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import sync_playwright

from douyin_dl.core import UA

# ---------------------------------------------------------------------------
# 注入脚本
#
# 注意: Object.defineProperty 使用 getter/setter 时 **不能** 同时给
# writable —— 否则抛 "Invalid property descriptor", 钩子会静默失效。
# 这个坑踩过一次, 这里保持正确写法。
# ---------------------------------------------------------------------------
HOOK_VER = 7   # 与 HOOK_JS 里的 const HOOK_VER 保持一致

HOOK_JS = r"""
(() => {
  // 版本号: 改动钩子逻辑就 +1, 否则页面里的旧版本会靠守卫把新版本挡掉(踩过坑)
  const HOOK_VER = 7;
  if (window.__DY_HOOK_VER__ === HOOK_VER) return;
  // 需要搭便车的接口
  const DATA_EP = [
    "/aweme/v1/web/aweme/detail",         // 视频详情(码率最全)
    "/aweme/v1/web/aweme/listcollection", // 收藏列表
    "/aweme/v1/web/aweme/post",           // 用户作品
    "/aweme/v1/web/tab/feed",             // 信息流
    "/aweme/v1/web/aweme/related",        // 相关推荐
    "/aweme/v1/web/mix/aweme",            // 合集
  ];
  // 收藏动作
  const ACT_EP = [
    "/aweme/v1/web/aweme/collect",
    "/aweme/v1/web/commit/item",
    "/aweme/v1/web/aweme/favorite",
  ];

  const hit = (u, arr) => !!u && arr.some((k) => u.includes(k));

  // 记录「用户真的点了收藏按钮」的时刻。
  // 页面加载时的同步请求和用户点击区分不开, 靠这个时间戳来判真。
  window.__dyFavAt = 0;
  // 最近点过收藏的视频 id(新的在前) + 每个 id 的点击时间。
  // 必须保留多个: 连续快速收藏两个视频时, 第一条请求回到 Python 时
  // 「最后一次点击」可能已经变成第二个视频了, 只存一个会把前一个漏掉。
  window.__dyFavIds = [];
  window.__dyFavTs = {};

  const ID_RE = new RegExp("[0-9]{15,}");
  const pickId = (s) => { const m = s && s.match(ID_RE); return m ? m[0] : ""; };

  const idFromNode = (el) => {
    // 1) 抖音详情页的播放容器上有 data-e2e-vid, 这是最可靠的
    for (let i = 0; i < 12 && el; i++, el = el.parentElement) {
      const v = el.getAttribute && el.getAttribute("data-e2e-vid");
      if (v) return v;
    }
    // 2) 退而求其次: 往上找带 id 的属性 / 链接 / data-e2e
    let n = el;
    for (let i = 0; i < 12 && n; i++, n = n.parentElement) {
      const a = (n.getAttribute && (n.getAttribute("data-e2e") || "")) || "";
      const id = n.getAttribute && (n.getAttribute("data-id") || n.getAttribute("data-aweme-id"));
      if (id && pickId(id)) return pickId(id);
      const href = n.href || (n.getAttribute && n.getAttribute("href")) || "";
      if (href) {
        const m = href.match(new RegExp("/(?:video|note)/([0-9]{15,})"));
        if (m) return m[1];
      }
      const got = pickId(a);
      if (got) return got;
    }
    // 3) 最后从页面地址里取, 但必须保证是当前播放的那个
    const cur = location.href.match(new RegExp("/(?:video|note)/([0-9]{15,})"));
    return cur ? cur[1] : "";
  };

  document.addEventListener("click", (ev) => {
    try {
      let el = ev.target;
      let hit = false;
      let mark = "";
      for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
        // 抖音的收藏按钮: data-e2e="video-player-collect", 里面只有个图标和数字(收藏数),
        // 既没有 aria-label 也没有「收藏」两个字, 只能按 data-e2e 认。
        const e2e = (el.getAttribute && el.getAttribute("data-e2e")) || "";
        if (/collect|favorite/i.test(e2e)) { hit = true; mark = "e2e:" + e2e; break; }
        const txt = ((el.innerText || "") + " " + (el.getAttribute?.("aria-label") || "")).trim();
        if (txt && txt.length <= 12 && /收藏/.test(txt)) { hit = true; mark = "text:" + txt; break; }
      }
      if (!hit) return;
      const now = Date.now();
      window.__dyFavAt = now;
      const id = idFromNode(ev.target);
      if (id) {
        window.__dyFavTs[id] = now;
        const prev = (window.__dyFavIds || []).filter((x) => x !== id);
        window.__dyFavIds = [id, ...prev]
          .filter((x) => now - (window.__dyFavTs[x] || 0) < 600000)
          .slice(0, 20);
      }
      // 记录按钮当时的「长相」: 抖音收藏后图标会变色/换类名。
      // 用户说「我明明没收藏」时, 靠它判断浏览器当时是不是把它当成了已收藏。
      let look = "";
      try {
        const btn = (ev.target.closest && ev.target.closest('[data-e2e*="collect"]')) || null;
        if (btn) {
          look = [
            (btn.className || "").toString().slice(0, 80),
            btn.getAttribute("aria-pressed") || "",
            btn.getAttribute("data-e2e-state") || "",
            btn.getAttribute("data-state") || "",
            (btn.querySelector("svg") ? (btn.querySelector("svg").getAttribute("fill") || "") : ""),
          ].join(" | ");
        }
      } catch (e) {}
      // 诊断用: 把「确实点到了收藏按钮」这件事回报给 Python。
      // 如果点了没下载, 看日志里有没有这一行, 就能判断是钩子没装/没认出来, 还是接口没触发。
      send("click", location.href,
           JSON.stringify({ mark: mark, id: id || "", look: look }), "");
    } catch (e) {}
  }, true);

  // 传输层用 Playwright 的 expose_binding: 页面直接调用 Python 函数。
  // 不走网络, 因此不会被抖音 CSP 的 connect-src 拦掉(用 fetch 打本地端口会 ERR_FAILED)。
  const send = (kind, url, text, reqBody) => {
    try {
      if (text && text.length > 8 * 1024 * 1024) text = text.slice(0, 4096);
      if (typeof window.__dyReport !== "function") return;
      window.__dyReport(JSON.stringify({
        kind: kind, url: url, body: text || "", req: reqBody || "",
        href: location.href,
        favAt: window.__dyFavAt || 0,
        favIds: window.__dyFavIds || [],
        favTs: window.__dyFavTs || {},
      }));
    } catch (e) {}
  };

  const classify = (url) => {
    if (hit(url, ACT_EP)) return "action";
    if (hit(url, DATA_EP)) return "data";
    return null;
  };

  // ---------- fetch ----------
  const realFetch = window.fetch.bind(window);
  const onFetch = function (...args) {
    const req = args[0];
    const url = typeof req === "string" ? req : (req && req.url) || "";
    const kind = classify(url);
    let reqBody = "";
    if (kind) {
      // 两种写法都要抓: fetch(new Request(...)) 和 fetch(url, {method, body})
      const init = args[1] || {};
      if (typeof init.body === "string") reqBody = init.body;
      else if (typeof req !== "string" && req && typeof req.clone === "function") {
        try { req.clone().text().then((t) => { reqBody = t; }).catch(() => {}); } catch (e) {}
      }
    }
    const p = realFetch(...args);
    if (kind) {
      p.then((res) => {
        try {
          res.clone().text().then((t) => send(kind, url, t, reqBody)).catch(() => {});
        } catch (e) {}
      }).catch(() => {});
    }
    return p;
  };
  try {
    Object.defineProperty(window, "fetch", {
      configurable: true,
      enumerable: true,
      get: () => onFetch,
      set: () => {},   // 页面若想覆盖, 忽略之
    });
  } catch (e) {
    window.fetch = onFetch;
  }

  // ---------- XMLHttpRequest ----------
  const XP = XMLHttpRequest.prototype;
  const realOpen = XP.open;
  const realSend = XP.send;
  Object.defineProperty(XP, "open", {
    configurable: true, writable: true,
    value: function (method, url, ...rest) {
      try { this.__dy_url = url; } catch (e) {}
      return realOpen.call(this, method, url, ...rest);
    },
  });
  Object.defineProperty(XP, "send", {
    configurable: true, writable: true,
    value: function (body) {
      let url = "";
      try { url = this.__dy_url || ""; } catch (e) {}
      const kind = classify(url);
      if (kind) {
        this.addEventListener("load", () => {
          try {
            let t = "";
            if (this.responseType === "" || this.responseType === "text") t = this.responseText;
            else if (this.responseType === "json") t = JSON.stringify(this.response);
            else if (this.responseType === "arraybuffer") t = new TextDecoder().decode(this.response);
            send(kind, url, t, typeof body === "string" ? body : "");
          } catch (e) {}
        });
      }
      return realSend.call(this, body);
    },
  });

  window.__DY_HOOK_VER__ = HOOK_VER;
  window.__DY_HOOKED_V2__ = true;   // 兼容旧判断
  console.log("[douyin-dl] hook v" + HOOK_VER + " installed");
})();
"""


# ---------------------------------------------------------------------------
# 从响应里挖 aweme
# ---------------------------------------------------------------------------

def extract_awemes(obj: Any) -> list[dict]:
    """递归找出所有 aweme 对象(带 aweme_id 且有 video/images/desc)。"""
    found: list[dict] = []

    def walk(node: Any, depth: int = 0):
        if depth > 12:
            return
        if isinstance(node, dict):
            if "aweme_id" in node and ("video" in node or "images" in node):
                found.append(node)
                return
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(obj)
    return found


def collect_ids(obj: Any) -> list[str]:
    """
    从收藏动作的响应/请求体里找出 aweme_id。
    抖音这几个接口结构不太统一, 这里广撒网。
    """
    ids: list[str] = []

    def add(v):
        s = str(v or "").strip()
        if s.isdigit() and len(s) >= 15 and s not in ids:
            ids.append(s)

    def walk(node: Any, depth: int = 0):
        if depth > 8:
            return
        if isinstance(node, dict):
            for k in ("aweme_id", "awemeId", "item_id", "itemId", "object_id"):
                if k in node:
                    add(node[k])
            # 列表型字段
            for k in ("aweme_ids", "item_ids", "aweme_list"):
                v = node.get(k)
                if isinstance(v, list):
                    for x in v:
                        if isinstance(x, (str, int)):
                            add(x)
                        else:
                            walk(x, depth + 1)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(obj)
    return ids


def ids_from_url(url: str) -> list[str]:
    """收藏接口常常是 GET, 目标 id 和 action 都在 query 里。"""
    ids: list[str] = []
    try:
        qs = url.split("?", 1)[1] if "?" in url else ""
    except Exception:
        return ids
    for k, v in parse_qsl(qs, keep_blank_values=False):
        if k.lower() in ("aweme_id", "item_id", "object_id", "group_id"):
            s = str(v).strip()
            if s.isdigit() and len(s) >= 15 and s not in ids:
                ids.append(s)
    return ids


def action_from_url(url: str) -> str:
    """从 query 里取 action。取不到返回空串。"""
    try:
        qs = url.split("?", 1)[1] if "?" in url else ""
    except Exception:
        return ""
    for k, v in parse_qsl(qs, keep_blank_values=False):
        if k.lower() in ("action", "collect_action"):
            return str(v).strip()
    return ""


def action_value(url: str, body: Any = None, req: Any = None) -> str:
    """
    这次收藏接口调用到底在干什么。

    实测(2026-11-10, 用 data-e2e-state 对照验证过):
        action=1  ->  收藏   (点前 video-player-no-collect, 点后 video-player-is-collected)
        action=0  ->  取消收藏
    注意别再按直觉反过来写: 早期就是把它当反的, 导致每次真收藏都被当成取消跳过。

    返回 "1"/"0", 或者空串(这类请求是页面自己发的状态同步, 不带 action)。
    """
    a = action_from_url(url or "")
    if not a:
        for src in (body, req):
            if isinstance(src, dict):
                for k in ("action", "collect_action"):
                    v = src.get(k)
                    if v is not None and str(v) in ("0", "1"):
                        a = str(v)
                        break
            if a:
                break
    return a


def is_uncollect(url: str, body: Any, req: Any) -> bool:
    """是不是「取消收藏」。只有明确带 action=0 才算; 没有 action 的同步请求不算取消。"""
    return action_value(url, body, req) == "0"


# ---------------------------------------------------------------------------
# Chrome 管理
# ---------------------------------------------------------------------------

def find_chrome() -> str | None:
    cands = []
    env = os.environ.get("CHROME_PATH")
    if env:
        cands.append(env)
    cands += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


class BrowserWatcher:
    """启动/连接 Chrome, 持续监听收藏。"""

    FAV_URL = "https://www.douyin.com/user/self?showTab=favorite_collection"

    def __init__(
        self,
        profile_dir: Path,
        port: int = 9222,
        on_aweme: Callable[[dict, str], None] | None = None,
        on_cookie: Callable[[str], None] | None = None,
        log=print,
        headless: bool = False,
        on_status: Callable[[dict], None] | None = None,
    ):
        self.profile_dir = Path(profile_dir)
        self.port = port
        self.on_aweme = on_aweme or (lambda a, s: None)
        self.on_status = on_status or (lambda kw: None)
        self.on_cookie = on_cookie or (lambda c: None)
        self.log = log
        self.headless = headless
        self.proc: subprocess.Popen | None = None

        # aweme_id -> aweme 对象 (从任何流量里缓存)
        self.cache: dict[str, dict] = {}
        self.cache_order: list[str] = []
        self.cache_limit = 400

        # 收藏列表基线: 启动后第一次 listcollection 只记录不下载
        self.baseline_done = False
        self.known_favs: set[str] = set()

        self.download_all_on_start = False
        self._lock = threading.Lock()
        self._pending_actions: list[str] = []
        self._page = None
        self._ctx = None
        self._browser = None
        # 页面 -> Python 的事件队列(binding 回调里只入队, 下载在主线程做)
        self.queue: queue.Queue = queue.Queue()
        self.seen_payloads = 0
        self.start_time = time.time()
        # 无头取详情用的后台线程(不再开任何窗口)
        self._hl_thread: threading.Thread | None = None
        self._hl_in: queue.Queue | None = None
        self._hl_out: queue.Queue | None = None
        self._resolving: set[str] = set()

    # -- 缓存 --------------------------------------------------------------
    def _remember(self, awemes: list[dict]):
        with self._lock:
            for aw in awemes:
                aid = str(aw.get("aweme_id") or "")
                if not aid:
                    continue
                self.cache[aid] = aw
                if aid not in self.cache_order:
                    self.cache_order.append(aid)
            while len(self.cache_order) > self.cache_limit:
                old = self.cache_order.pop(0)
                self.cache.pop(old, None)

    def _get_cached(self, aid: str) -> dict | None:
        with self._lock:
            return self.cache.get(aid)

    # -- 回调 --------------------------------------------------------------
    def _on_binding(self, source, payload):
        """Playwright binding: 钩子把 JSON 字符串递进来。只入队, 不做重活。"""
        try:
            p = json.loads(payload) if isinstance(payload, str) else (payload or {})
            self.seen_payloads += 1
            self.queue.put((
                p.get("kind") or "",
                p.get("url") or "",
                p.get("body") or "",
                p.get("req") or "",
                {
                    "href": p.get("href") or "",
                    "favAt": p.get("favAt") or 0,
                    "favIds": p.get("favIds") or [],
                    "favTs": p.get("favTs") or {},
                },
            ))
        except Exception:
            pass

    def drain(self) -> int:
        """把队列里的事件全部处理掉, 返回处理条数。"""
        n = 0
        while True:
            try:
                kind, url, body, req, meta = self.queue.get_nowait()
            except queue.Empty:
                break
            n += 1
            try:
                self._on_payload(kind, url, body, req, meta)
            except Exception as e:
                self.log(f"  ! 处理事件出错: {e}")
        return n

    def _is_first_page(self, url: str) -> bool:
        """收藏列表是不是「第一页」的请求(cursor 缺省或为 0)。"""
        try:
            q = url.split("?", 1)[1] if "?" in url else ""
            params = dict(
                (kv.split("=", 1) + [""])[:2] for kv in q.split("&") if kv
            )
        except Exception:
            return True
        for key in ("cursor", "max_cursor", "offset"):
            if key in params and params[key] not in ("", "0"):
                return False
        return True

    def _looks_fresh(self, aw: dict) -> bool:
        """
        收藏时间是否就在本次会话期间。
        没有 collect_time 字段时保守放行(抖音不是每个响应都带)。
        """
        for key in ("collect_time", "collect_time_ms", "favorite_time"):
            v = aw.get(key)
            if isinstance(v, (int, float)) and v > 0:
                ts = v / 1000 if v > 1e12 else v
                return ts >= self.start_time - 300
        return True

    def _current_video_id(self, href: str) -> str:
        """从当前页面地址里取「正在看的那个视频」的 id。"""
        if not href:
            return ""
        m = re.search(r"/(?:video|note)/(\d{15,})", href)
        if m:
            return m.group(1)
        m = re.search(r"[?&]modal_id=(\d{15,})", href)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _parse_body(text: str) -> Any:
        """请求体可能是 JSON, 也可能是 form-urlencoded。抖音的收藏接口用的是后者:
        `action=0&aweme_id=7680805165196188276&aweme_type=0`
        只按 JSON 解析会得到空字典, id 就丢了 —— 这个坑踩过。"""
        if not text:
            return {}
        t = text.strip()
        if t[:1] in "{[":
            try:
                return json.loads(t)
            except Exception:
                return {}
        if "=" in t:
            try:
                return dict(parse_qsl(t, keep_blank_values=True))
            except Exception:
                return {}
        return {}

    def _on_payload(self, kind: str, url: str, body_text: str,
                    req_text: str, meta: dict = None):
        body = self._parse_body(body_text)
        req = self._parse_body(req_text)

        if kind == "click":
            # 诊断: 确认「用户点了收藏按钮」这件事有没有走到 Python。
            # 日志里有这行但没下载 -> 接口/闸门的问题; 连这行都没有 -> 钩子没装或没认出按钮。
            try:
                info = json.loads(body_text) if (body_text or "").strip().startswith("{") else {}
            except Exception:
                info = {}
            cid = str(info.get("id") or "")
            mark = str(info.get("mark") or "")
            look = str(info.get("look") or "")
            self.log(f"  · 检测到收藏点击 [{mark or '?'}] 视频={cid or '未知'}")
            if look:
                self.log(f"    按钮状态: {look[:150]}")
            return

        if kind == "data":
            awemes = extract_awemes(body)
            if not awemes:
                return
            self._remember(awemes)

            if "listcollection" in url:
                # 只信「第一页(cursor=0)」的出现顺序:
                # 收藏列表是按收藏时间倒序的, 真正的新收藏只会出现在第一页的最前面。
                # 翻页(cursor!=0)加载出来的全是历史收藏, 绝不能当新收藏下载。
                if not self._is_first_page(url):
                    return

                ids = [str(a.get("aweme_id")) for a in awemes if a.get("aweme_id")]

                with self._lock:
                    fresh_baseline = not self.baseline_done
                    self.baseline_done = True
                    self.known_favs.update(ids)

                if fresh_baseline:
                    if self.download_all_on_start:
                        self.log(f"  收藏基线: {len(ids)} 个, 开始回补下载")
                        for aid in ids:
                            aw = self._get_cached(aid)
                            if aw:
                                self.on_aweme(aw, "收藏列表")
                    else:
                        self.log(f"  收藏基线: 已记录 {len(ids)} 个已有收藏(只下新的)")

                # 收藏列表只用来建基线/缓存候选数据, 绝不作为下载触发。
                # 原因: 第一页内容会自己轮换, 靠列表比对会把旧收藏当成新收藏重下。
                # 真正的触发只认 action 分支里「用户亲手点收藏」那条路。

        elif kind == "action":
            # 记下原始请求, 排查「我明明点了收藏, 日志却说取消」时要靠它
            _a = action_from_url(url)
            if not _a and isinstance(req, dict):
                _a = str(req.get("action", "") or req.get("collect_action", ""))
            self.log(f"  · 收藏接口 action={_a or '?'} "
                     f"req={req_text[:120] if req_text else '(空)'}")
            if is_uncollect(url, body, req):
                _u = collect_ids(req) or ids_from_url(url) or ["?"]
                self.log(f"  · 取消收藏 {_u[0]} (不下载)")
                return
            # 请求体里带的是「这一次操作的那个视频」, 最准确;
            # 响应体里常常带着整个收藏列表, 拿来当目标会误下, 只在前者为空时兜底。
            ids = collect_ids(req) or ids_from_url(url) or collect_ids(body)[:1]
            if not ids:
                return
            for aid in ids[:1]:
                # 只认明确带 action 的请求:
                #   实测页面自己发的状态同步请求不带 action(req 是 aweme_id=..&item_type=0&type=0),
                #   带 action=1 的一定是人点了收藏。
                # 早期用「必须匹配到点击记录」来挡同步请求, 结果连真实点击也一起挡了
                # (连点两个视频、或按钮在详情浮层里时, 点击记录对不上号) —— 已废弃。
                _act = action_value(url, body, req)
                if _act != "1":
                    self.log(f"  · 忽略状态同步请求 {aid} (没有 action)")
                    return
                # 一律重新抓一次详情, 不吃缓存:
                # 缓存(尤其是收藏列表接口带来的)里的播放地址签名很快失效,
                # 直接拿去下载会整个视频所有候选都 403。实测重新抓详情后同一个视频立刻能下。
                with self._lock:
                    if aid not in self._pending_actions:
                        self._pending_actions.append(aid)
                self.log(f"  · 新收藏 {aid}, 正在抓取最新视频信息…")

    # -- 补全: 用无头浏览器取详情 -------------------------------------------
    def _hl_loop(self):
        """
        后台线程: 用一个无头 Chrome 打开视频页, 抓页面自己发出的详情接口。

        为什么要有这条线程: Playwright 的 sync API 必须待在创建它的线程里,
        所以这里单独 start() 一个 playwright 实例, 和主循环完全隔离。
        好处是整个过程没有任何窗口, 用户眼前绝对不会跳东西。
        """
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    channel="chrome",
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--mute-audio",
                        "--disable-gpu",
                        "--no-first-run",
                    ],
                )
                ctx = browser.new_context(
                    user_agent=UA,
                    viewport={"width": 1280, "height": 800},
                    locale="zh-CN",
                )
                page = ctx.new_page()
                while True:
                    item = self._hl_in.get()
                    if item is None:            # 退出信号
                        break
                    aid, cookies = item
                    aw = None
                    try:
                        aw = self._hl_fetch(page, ctx, aid, cookies)
                    except Exception as e:
                        self.log(f"  ! 无头取详情失败 {aid}: {str(e)[:60]}")
                    self._hl_out.put((aid, aw))
                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            self.log(f"  ! 无头浏览器启动失败: {str(e)[:90]}")
            # 让所有排队的请求都拿到 None, 否则主循环会一直等
            while True:
                try:
                    aid, _ = self._hl_in.get_nowait()
                except queue.Empty:
                    break
                if aid is None:
                    break
                self._hl_out.put((aid, None))

    def _hl_fetch(self, page, ctx, aid, cookies):
        """在无头页面里打开视频, 等 /aweme/detail/ 返回带 aweme_detail 的 JSON。"""
        try:
            ctx.clear_cookies()
            if cookies:
                ctx.add_cookies(cookies)
        except Exception:
            pass

        got: dict[str, Any] = {}

        def on_resp(r):
            if "aweme" in got or "/aweme/detail/" not in r.url:
                return
            try:
                data = r.json()
            except Exception:
                return
            ad = data.get("aweme_detail")
            if isinstance(ad, dict) and (ad.get("video") or ad.get("images")):
                got["aweme"] = ad

        page.on("response", on_resp)
        try:
            page.goto(f"https://www.douyin.com/video/{aid}",
                      wait_until="domcontentloaded", timeout=30000)
            for _ in range(24):                  # 最多等 12 秒
                if "aweme" in got:
                    break
                page.wait_for_timeout(500)
        finally:
            try:
                page.remove_listener("response", on_resp)
            except Exception:
                pass
        return got.get("aweme")

    def _ensure_hl_worker(self):
        if self._hl_thread is not None and self._hl_thread.is_alive():
            return
        self._hl_in = queue.Queue()
        self._hl_out = queue.Queue()
        self._hl_thread = threading.Thread(target=self._hl_loop,
                                           name="dy-headless", daemon=True)
        self._hl_thread.start()

    def _cleanup_stale_bg(self, ctx):
        """
        清理旧版本遗留在屏幕外的补全窗口。

        旧方案靠页面里的 window.__DY_BG__ 标记识别, 但页面一导航标记就没了,
        于是窗口会越积越多(还可能被恢复成 normal 状态)。现在改成看窗口坐标:
        留在屏幕左侧(x <= -2000)的窗口一定是我们的残留, 直接关掉。
        """
        if getattr(self, "_browser", None) is None:
            return
        try:
            cdp = self._browser.new_browser_cdp_session()
            found = []
            for t in cdp.send("Target.getTargets")["targetInfos"]:
                if t.get("type") != "page":
                    continue
                try:
                    w = cdp.send("Browser.getWindowForTarget",
                                 {"targetId": t["targetId"]})
                    bd = cdp.send("Browser.getWindowBounds",
                                  {"windowId": w["windowId"]})["bounds"]
                except Exception:
                    continue
                if int(bd.get("left") or 0) <= -2000:
                    found.append(t["targetId"])
            for tid in found:
                try:
                    cdp.send("Target.closeTarget", {"targetId": tid})
                except Exception:
                    pass
            if found:
                self.log(f"  清理了 {len(found)} 个遗留的后台窗口")
        except Exception:
            pass

    def _ensure_douyin_tab(self, ctx):
        """
        保证浏览器里有一个能刷的抖音页面。

        踩过的坑: 为了"不打扰用户"把启动时的 goto 删了, 结果冷启动后窗口里只有
        一个 about:blank —— 用户看到的是白屏, 以为浏览器坏了。
        这里的规矩: 已经有抖音页就一个字都不动; 全都没有才自己开一个。
        """
        for pg in list(ctx.pages):
            try:
                if "douyin.com" in (pg.url or ""):
                    return pg
            except Exception:
                continue

        page = None
        for pg in list(ctx.pages):          # 优先复用空白页, 不再多开标签
            try:
                if (pg.url or "").startswith("about:"):
                    page = pg
                    break
            except Exception:
                continue
        if page is None:
            try:
                page = ctx.new_page()
            except Exception as e:
                self.log(f"  ! 打不开标签页: {str(e)[:50]}")
                return None

        try:
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                      timeout=60000)
        except Exception as e:
            self.log(f"  ! 打开抖音失败: {str(e)[:60]}")
            return page
        time.sleep(2)
        self._install_hook(page)
        return page

    def _close_bg(self):
        """停掉无头解析线程(退出时调用)。"""
        try:
            if self._hl_in is not None:
                self._hl_in.put(None)
            if self._hl_thread is not None:
                self._hl_thread.join(timeout=5)
        except Exception:
            pass
        self._hl_thread = None

    def _resolve_pending(self):
        """
        把「点了收藏但还没拿到视频数据」的条目补齐。

        现在全部交给无头浏览器线程去取, 本进程不再开任何窗口 ——
        以前那种"后台窗口"方案会留下一堆看不见的 Chrome 窗口, 还会把窗口状态
        从 minimized 变回 normal, 在任务栏里显形。
        """
        with self._lock:
            pend = [a for a in self._pending_actions if a not in self._resolving]
            self._pending_actions.clear()

        if not pend or self._ctx is None:
            return

        # 一轮最多补几个, 剩下的下一轮再来, 避免长时间卡住主循环
        batch, rest = pend[:3], pend[3:]
        if rest:
            with self._lock:
                self._pending_actions.extend(rest)

        # 故意不看缓存: 只为了拿一份「现在还有效」的播放地址
        todo = list(batch)

        self._ensure_hl_worker()
        if self._hl_thread is None or not self._hl_thread.is_alive():
            with self._lock:
                self._pending_actions.extend(todo)
            return

        cookies = self._douyin_cookies()
        for aid in todo:
            self._resolving.add(aid)
            self._hl_in.put((aid, cookies))

        # 等结果: 期间照常处理页面事件, 保证点击不会漏
        deadline = time.time() + 45
        while self._resolving and time.time() < deadline:
            try:
                aid, aw = self._hl_out.get(timeout=0.5)
            except queue.Empty:
                try:
                    self.pump(self._ctx)
                    self.drain()
                except Exception:
                    pass
                continue
            self._resolving.discard(aid)
            if aw:
                self.on_aweme(aw, "收藏")
            else:
                old = self._get_cached(aid)      # 抓不到就退回旧数据, 聊胜于无
                if old:
                    self.log(f"  ! 抓不到最新数据, 先用旧缓存试一次 {aid}")
                    self.on_aweme(old, "收藏")
                else:
                    self.log(f"  ! 拿不到 {aid} 的视频信息")

        if self._resolving:                      # 超时: 放回队列下轮再来
            for aid in list(self._resolving):
                self._resolving.discard(aid)
                with self._lock:
                    self._pending_actions.append(aid)

    def _douyin_cookies(self):
        """把已登录浏览器里的抖音 Cookie 抠出来, 给无头浏览器用。"""
        out = []
        try:
            for c in self._ctx.cookies():
                if "douyin" not in (c.get("domain") or ""):
                    continue
                out.append({
                    "name": c["name"], "value": c["value"],
                    "domain": c["domain"], "path": c.get("path") or "/",
                    "expires": c.get("expires") or -1,
                    "httpOnly": bool(c.get("httpOnly")),
                    "secure": bool(c.get("secure")),
                    "sameSite": c.get("sameSite") or "Lax",
                })
        except Exception:
            pass
        return out

    # -- Chrome ------------------------------------------------------------
    def _probe_port(self) -> dict | None:
        """调试端口是否已经有一个可用的浏览器。"""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/version", timeout=1
            ) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def start_chrome(self) -> bool:
        # 已经有一个带调试端口的浏览器在跑(比如上次没关干净) —— 直接复用,
        # 否则新进程只会往旧实例里开个标签页, 端口永远起不来。
        info = self._probe_port()
        if info:
            self._browser_name = info.get("Browser", "")
            self.log(f"  复用已运行的浏览器: {info.get('Browser', '')}")
            return True

        chrome = find_chrome()
        if not chrome:
            self.log("! 找不到 Chrome / Edge。请安装 Chrome, 或设置环境变量 CHROME_PATH")
            return False
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        args = [
            chrome,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=Translate,OptimizationHints",
        ]
        if self.headless:
            args.append("--headless=new")
        args.append("about:blank")

        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/version", timeout=1
                ) as r:
                    info = json.loads(r.read())
                self.log(f"  浏览器已就绪: {info.get('Browser', '')}")
                return True
            except Exception:
                if self.proc.poll() is not None:
                    self.log("! 浏览器启动后立即退出(可能端口被占用)")
                    return False
                time.sleep(0.5)
        self.log("! 等待调试端口超时")
        return False

    @staticmethod
    def check_login(ctx) -> bool:
        try:
            names = {c["name"] for c in ctx.cookies()}
        except Exception:
            return False
        return bool({"sessionid", "sessionid_ss", "sid_tt"} & names)

    # -- 主循环 ------------------------------------------------------------
    def run(self):
        if not self.start_chrome():
            return

        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                self._ctx = ctx
                self._browser = browser

                # 传输通道: 页面里出现 window.__dyReport, 直接调用 Python
                ctx.expose_binding("__dyReport", self._on_binding)
                ctx.add_init_script(HOOK_JS)
                ctx.on("page", lambda pg: self._install_hook(pg))
                self._sync_cookies(ctx)

                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                self._page = page

                # 启动时千万别动用户正在看的页面。装钩子即可, 该刷抖音时页面自己会发请求。
                self._cleanup_stale_bg(ctx)
                for pg in list(ctx.pages):
                    try:
                        if not pg.is_closed():
                            self._install_hook(pg)
                    except Exception:
                        pass

                # 冷启动时窗口里可能只有一个 about:blank, 自动补一个抖音页
                page = self._ensure_douyin_tab(ctx) or page
                self._page = page

                if not self.check_login(ctx):
                    self.log("")
                    self.log("  ┌─────────────────────────────────────────────┐")
                    self.log("  │  请在弹出的浏览器窗口里扫码登录抖音          │")
                    self.log("  │  登录后会自动继续, 无需其他操作              │")
                    self.log("  └─────────────────────────────────────────────┘")
                    self.log("")
                    deadline = time.time() + 600
                    while time.time() < deadline:
                        time.sleep(2)
                        self.pump(ctx)
                        if self.check_login(ctx):
                            break
                    if not self.check_login(ctx):
                        self.log("! 登录等待超时, 退出")
                        return
                    self.log("  ✔ 登录成功")
                    time.sleep(2)
                    self._install_hook(page)

                self.log("")
                try:
                    self.on_status({"running": True,
                                    "browser": getattr(self, "_browser_name", "") or "已连接"})
                except Exception:
                    pass
                self.log("  ✔ 监听已启动")
                self.log("    现在去抖音正常刷、点『收藏』, 视频会自动下载到 downloads/")
                self.log("    按 Ctrl+C 退出")
                self.log("")

                # 只有回补历史收藏时才需要主动打开收藏页。
                # 平时绝不切走用户正在看的页面(启动就被强制跳转会很难用)。
                if self.download_all_on_start:
                    try:
                        page.goto(self.FAV_URL, wait_until="domcontentloaded",
                                  timeout=60000)
                    except Exception:
                        pass
                    time.sleep(3)
                    self.pump(ctx)
                    self._scroll_all(page)

                # 主循环: 排空事件 -> 补全待定 -> 保活钩子
                tick = 0
                err = 0
                reopen_fail = 0
                while True:
                    try:
                        # 用户关掉浏览器后, ctx 可能仍然"看着正常", 只有这个能可靠判断
                        if getattr(self, "_browser", None) is not None                                 and not self._browser.is_connected():
                            self.log("  ! 浏览器已经关掉了, 监听结束")
                            break
                        self.pump(ctx)             # 让 Playwright 收包(同时处理 binding 回调)
                        err = 0
                    except Exception as e:
                        # 用户把浏览器关掉时, 这里会连续抛错; 别装作还在跑
                        err += 1
                        if err >= 3:
                            self.log(f"  ! 浏览器已关闭或失去连接, 监听结束: {str(e)[:60]}")
                            break
                        time.sleep(0.5)
                        continue
                    self.drain()
                    if self._pending_actions:
                        self._resolve_pending()
                    tick += 1
                    if tick % 15 == 0:             # 每 ~6 秒巡检一次钩子是否还在
                        for pg in list(ctx.pages):
                            try:
                                if not pg.is_closed():
                                    self._install_hook(pg)
                            except Exception:
                                pass
                        self._sync_cookies(ctx)
                    if tick % 150 == 0:            # 每 ~60 秒: 窗口被关空了就补一个
                        try:
                            if not [x for x in ctx.pages if not x.is_closed()]:
                                self.log("  浏览器里没有页面了, 重新打开抖音…")
                                if self._ensure_douyin_tab(ctx) is not None:
                                    reopen_fail = 0
                                else:
                                    reopen_fail += 1
                                    if reopen_fail >= 2:
                                        self.log("  ! 浏览器已经不可用, 监听结束")
                                        break
                        except Exception:
                            reopen_fail += 1
                            if reopen_fail >= 2:
                                self.log("  ! 浏览器已经不可用, 监听结束")
                                break
                    time.sleep(0.4)

        except KeyboardInterrupt:
            self.log("\n  收到退出信号, 正在关闭…")
        except Exception as e:
            self.log(f"! 运行出错: {e}")
        finally:
            try:
                self.on_status({"running": False, "browser": ""})
            except Exception:
                pass
            self._close_bg()

    def _sync_cookies(self, ctx):
        """
        把浏览器里的登录 Cookie 同步给下载器。
        抖音的视频地址带 tk=webid, 是绑定登录态的; 不带 Cookie 直接 GET 会 403。
        """
        try:
            parts = []
            for c in ctx.cookies():
                dom = c.get("domain") or ""
                if "douyin" in dom or "douyin" in (c.get("url") or ""):
                    parts.append(f"{c['name']}={c['value']}")
            if parts:
                self.on_cookie("; ".join(parts))
        except Exception:
            pass

    def pump(self, ctx):
        """
        sync API 只在「真正和浏览器通信的那一刻」派发积压的事件。
        len(ctx.pages) 读的是本地缓存, 根本不走网络 —— 用它当 pump 是无效的,
        binding 回调会一直卡到下一次真正的 API 调用才被派发(踩过坑)。
        这里用 wait_for_timeout(1) 做一次极轻的往返, 把队列冲出来。
        """
        try:
            pages = ctx.pages
            if pages:
                pages[0].wait_for_timeout(1)
        except Exception:
            pass

    def _install_hook(self, page):
        # 页面正在跳转时 evaluate 会抛 "Execution context was destroyed", 属正常, 下一轮再装
        for _ in range(2):
            try:
                if page.evaluate("() => window.__DY_HOOK_VER__ || 0") != HOOK_VER:
                    page.evaluate(HOOK_JS)
                self._hook_ok = True
                return True
            except Exception:
                time.sleep(0.6)
        return False

    def _scroll_all(self, page):
        """把收藏列表滚到底, 触发分页加载。"""
        self.log("  正在加载全部收藏…")
        last = 0
        for i in range(120):
            try:
                h = page.evaluate("() => document.body.scrollHeight")
                page.mouse.wheel(0, 2400)
                time.sleep(1.2)
                if h == last and i > 3:
                    break
                last = h
            except Exception:
                break
        self.log(f"  已加载 {len(self.cache)} 个视频信息")
