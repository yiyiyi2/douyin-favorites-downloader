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

from douyin_dl.core import UA, PLATFORM_KUAISHOU, parse_kuaishou

# v1.7: 两个站点一视同仁。判「浏览器里还有没有页面」「要不要自动开页」
# 都得同时认这两个域名, 否则用户只刷快手时会被强开抖音页 / 被判关站。
_SITE_DOMAINS = ("douyin.com", "kuaishou.com")

# 主循环退出码。daemon.py 只把 EXIT_INTENTIONAL(7) 当"用户主动关, 别再拉起",
# 其他码一律按"崩了"处理并重启。裸 return 会返回 None -> sys.exit(None)=0,
# 从命令行跑时看着像"正常成功", 排障会被误导, 所以每个失败出口都给明确非零码。
EXIT_OK = 0                 # 正常结束
EXIT_BROWSER_FAIL = 2       # 浏览器起不来 / 连不上(和 --check 一致)
EXIT_LOGIN_TIMEOUT = 3      # 等扫码登录超时
EXIT_INTENTIONAL = 7        # 用户主动关闭(与 daemon.py 的 EXIT_INTENTIONAL 一致)

# 无界 set 的上限。跑几天几夜时 known_favs/_auto_submitted 会一直涨, 吃内存;
# 到顶就砍掉一批。集合无序, 被砍掉的 id 下次再出现会被当"新"的重新提交,
# 但 Downloader.submit 会按 .state.json 去重, 不会真的重下, 所以安全。
_SET_CAP_FAVS = 50000
_SET_CAP_AUTO = 20000
_SET_CAP_RESOLVING = 2000


def _cap_set(s: set, limit: int) -> int:
    """集合超过 limit 就删掉多出来的(任意顺序), 返回删除个数。"""
    over = len(s) - limit
    if over <= 0:
        return 0
    for x in list(s)[:over]:
        s.discard(x)
    return over


# ---------------------------------------------------------------------------
# 注入脚本
#
# 注意: Object.defineProperty 使用 getter/setter 时 **不能** 同时给
# writable —— 否则抛 "Invalid property descriptor", 钩子会静默失效。
# 这个坑踩过一次, 这里保持正确写法。
# ---------------------------------------------------------------------------
HOOK_VER = 11   # 与 HOOK_JS 里的 const HOOK_VER 保持一致(改钩子逻辑必须同步 +1)

HOOK_JS = r"""
(() => {
  // 版本号: 改动钩子逻辑就 +1, 否则页面里的旧版本会靠守卫把新版本挡掉(踩过坑)
  const HOOK_VER = 11;
  if (window.__DY_HOOK_VER__ === HOOK_VER) return;
  // 需要搭便车的接口
  const DATA_EP = [
    "/aweme/v1/web/aweme/detail",         // 视频详情(码率最全)
    "/aweme/v1/web/aweme/listcollection", // 收藏列表
    "/aweme/v1/web/aweme/post",           // 用户作品
    "/aweme/v1/web/tab/feed",             // 信息流
    "/aweme/v1/web/aweme/related",        // 相关推荐
    "/aweme/v1/web/mix/aweme",            // 合集
    // ---- v1.7 快手: /rest/v/... 风格, 形状和抖音完全不同 ----
    "/rest/v/feed/hot",                   // 推荐流(每条带完整 manifest 码率表)
    "/rest/v/profile/feed",               // 作者作品
    "/rest/v/collect/list",               // 我的收藏列表
    "/rest/v/feed/liked",                 // 我赞过的
    "/rest/v/search/feed",                // 搜索结果
  ];
  // 收藏动作
  const ACT_EP = [
    "/aweme/v1/web/aweme/collect",
    "/aweme/v1/web/commit/item",
    "/aweme/v1/web/aweme/favorite",
    // ---- v1.7 快手: 请求体 JSON {"photoId":..,"userId":..,"collect":1|2} ----
    // collect=1 收藏 / collect=2 取消收藏。和抖音的 1/0 不一样, 别照抄。
    "/rest/v/photo/collect",
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
  // 快手的 photoId 是短的首字母+数字串(如 3xb42hegyea3mzc), 抖音那套 15 位纯数字
  // 正则完全对不上, 单独来一条。路径里一般就长这样: /short-video/<id>
  const KS_RE = new RegExp("/(?:short-video|photo|fw/photo)/([0-9A-Za-z_-]{8,32})");
  const pickKs = (s) => { const m = s && s.match(KS_RE); return m ? m[1] : ""; };

  const idFromNode = (el) => {
    // v1.7: 下面的循环都会改 el(往上爬), 必须先把「原始点击元素」存一份。
    // 否则第 1 步走完 el 已经变成 null/document, 第 2/3 步从那个位置开始
    // 等于什么都没查 —— 抖音靠第 1 步的 data-e2e-vid 能命中所以没暴露,
    // 快手没有这个属性, 点击事件的 id 就会永远是空(踩过坑)。
    const start = el;
    // 1) 抖音详情页的播放容器上有 data-e2e-vid, 这是最可靠的
    for (let i = 0; i < 12 && el; i++, el = el.parentElement) {
      const v = el.getAttribute && el.getAttribute("data-e2e-vid");
      if (v) return v;
    }
    // 2) v1.7 快手: 卡片上常挂 data-photo-id / data-video-id, 第 1 步认不到时试这里
    let k = start;
    for (let i = 0; i < 12 && k; i++, k = k.parentElement) {
      if (!k.getAttribute) continue;
      const pid = k.getAttribute("data-photo-id") || k.getAttribute("data-video-id");
      if (pid) return pid;
      const href = k.href || k.getAttribute("href") || "";
      const kh = pickKs(href);
      if (kh) return kh;
    }
    // 3) 退而求其次: 往上找带 id 的属性 / 链接 / data-e2e(同样从原始元素开始)
    let n = start;
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
    // 4) 最后从页面地址里取, 但必须保证是当前播放的那个
    const cur = location.href.match(new RegExp("/(?:video|note)/([0-9]{15,})"));
    if (cur) return cur[1];
    return pickKs(location.href) || "";
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
        // v1.7 快手: 没有 data-e2e, 收藏按钮靠 class / aria-label 认
        // (实测类名里带 collect, 或者 title/aria-label 直接写「收藏」)。
        const cls = (el.className || "").toString();
        const lbl = (el.getAttribute?.("aria-label") || el.getAttribute?.("title") || "");
        if (/collect/i.test(cls) && cls.length < 200) { hit = true; mark = "cls:" + cls.slice(0, 40); break; }
        if (lbl && lbl.length <= 12 && /收藏/.test(lbl)) { hit = true; mark = "lbl:" + lbl; break; }
        const txt = ((el.innerText || "") + " " + lbl).trim();
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
        const btn = (ev.target.closest &&
          ev.target.closest('[data-e2e*="collect"], [class*="collect"], [class*="Collect"]')) || null;
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
      // v1.6: 千万别把大 JSON 截断成前 4096 字符 —— 那会变成无效 JSON,
      // Python 侧 extract_awemes 直接返回空, 收藏列表基线/缓存全失效(踩过坑)。
      // 收藏列表/详情几 MB 很常见, 整份传过去就行(expose_binding 是字符串通道)。
      // 只有极端异常(>32MB)才放弃正文并打标记, 让 Python 记一行日志而不是静默。
      if (text && text.length > 32 * 1024 * 1024) {
        kind = "oversize";
        text = "";
      }
      // v1.7.2: 半死页(快手 new-reco)的 Playwright expose_binding 通道会断,
      // 但 raw CDP Runtime.addBinding 注入的 __dyReportRaw 走独立 websocket,
      // 依然有效。优先走 raw 通道, 不行再退回 expose_binding。
      const payload = JSON.stringify({
        kind: kind, url: url, body: text || "", req: reqBody || "",
        href: location.href,
        favAt: window.__dyFavAt || 0,
        favIds: window.__dyFavIds || [],
        favTs: window.__dyFavTs || {},
      });
      if (typeof window.__dyReportRaw === "function") {
        try { window.__dyReportRaw(payload); return; } catch (e) {}
      }
      if (typeof window.__dyReport !== "function") return;
      window.__dyReport(payload);
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
    // v1.6: 请求体可能要异步读(Request.clone().text()), 必须和响应正文一起
    // Promise.all 等齐再上报。旧写法里 reqBody 由另一个 then 异步回填,
    // 响应先回来时 send 拿到的还是空串 -> POST 收藏漏下载(踩过坑)。
    let bodyPromise = Promise.resolve("");
    if (kind) {
      // 两种写法都要抓: fetch(new Request(...)) 和 fetch(url, {method, body})
      const init = args[1] || {};
      if (typeof init.body === "string") bodyPromise = Promise.resolve(init.body);
      else if (typeof req !== "string" && req && typeof req.clone === "function") {
        try { bodyPromise = req.clone().text(); } catch (e) {}
      }
    }
    const p = realFetch(...args);
    if (kind) {
      p.then((res) => {
        try {
          Promise.all([res.clone().text(), bodyPromise]).then((pair) => {
            send(kind, url, pair[0], pair[1] || "");
          }).catch(() => {});
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

# v1.6: 抖音的字段名一直在变。把这些候选名集中管理, 别在四个函数里各写一套,
# 免得 core.py 支持 awemeId 而 watcher.py 只认 aweme_id, 直接漏下载。
_ID_KEYS = ("aweme_id", "awemeId", "item_id", "itemId", "object_id", "group_id", "id")
_VIDEO_KEYS = ("video", "images", "image_list", "video_info", "play_addr", "aweme_detail")
_ID_LIST_KEYS = ("aweme_ids", "item_ids", "aweme_list", "awemeIdList", "ids")

# v1.7 快手: id 字段叫 photoId / photo_id, 视频信息叫 manifest / photoUrl。
# 单列一套, 别和抖音那三个常量混在一起(混了会把 photo 对象当 aweme 抽错)。
_KS_ID_KEYS = ("photoId", "photo_id", "id")
_KS_VIDEO_KEYS = ("manifest", "manifestH265", "photoUrl", "photoUrls", "coverUrl")


def _is_ks_id(v: Any) -> bool:
    """是不是快手那种 photoId。抖音是纯数字, 快手是字母数字混排。"""
    s = str(v or "").strip()
    if not (8 <= len(s) <= 32):
        return False
    if not all(c.isalnum() or c in "_-" for c in s):
        return False
    return any(c.isdigit() for c in s) and any(c.isalpha() for c in s)


def _norm_id(v: Any) -> str:
    """把任意形态的 id 归一化成字符串。抖音 id 现在是 19 位数字, 但将来
    可能带前缀/变短/变字符串 —— 别写死 len>=15, 只要"看着像个 id"就收。"""
    if isinstance(v, bool):
        return ""
    # v1.6: 容器类型的 str() 会变成 "['a']"/"[1]" 这种看着像 id 的垃圾
    # (无空格、长度合规, 旧版会把它当成一个真 id 收进去)。先按类型挡掉。
    if isinstance(v, (dict, list, tuple, set)):
        return ""
    if isinstance(v, float):
        if not (v == v and abs(v) < 1e30):   # NaN/Inf
            return ""
        v = int(v)
    s = str(v or "").strip()
    if not s:
        return ""
    # 纯数字 id: 不再写死长度(老代码 len>=15 会把测试/短 id/将来变短的 id 全挡掉)。
    # 这些值只会出现在明确的 id 键里, 短一点也不必怕误判。
    if s.isdigit():
        return s
    # 非纯数字(将来换成带前缀/哈希的 id): 只要不是超长垃圾就收
    return s if 1 <= len(s) <= 64 and not any(c.isspace() for c in s) else ""


def _action_norm(v: Any) -> str:
    """把 action 归一化成 "1"(收藏)/"0"(取消)/""(无关)。

    v1.6: 以前只认 str(v) in ("0","1"), 布尔 True 变 "True" 不匹配,
    字符串 "collect"/"favorite" 也不认 —— 接口一改就整条收藏链没反应。"""
    if v is None or isinstance(v, (dict, list)):
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    s = str(v).strip().lower()
    if s in ("1", "true", "collect", "favorite", "favourite", "add", "like", "yes"):
        return "1"
    if s in ("0", "false", "uncollect", "unfavorite", "cancel", "remove", "delete", "no"):
        return "0"
    return ""


def extract_awemes(obj: Any) -> list[dict]:
    """递归找出所有 aweme 对象(id 字段 + 视频/图集字段)。

    v1.6: 闸门放宽 —— id 认别名, 视频认别名; 即使将来 aweme 被包一层、
    或 video 改名, 也照样能把整条候选挖出来, 不至于"以后一变就全不下"。"""
    found: list[dict] = []
    seen: set[int] = set()

    def looks_like_aweme(node: dict) -> bool:
        has_id = any(k in node and _norm_id(node.get(k)) for k in _ID_KEYS)
        has_video = any(k in node for k in _VIDEO_KEYS)
        return has_id and has_video

    def walk(node: Any, depth: int = 0):
        if depth > 14:
            return
        if isinstance(node, dict):
            if looks_like_aweme(node):
                if id(node) not in seen:
                    seen.add(id(node))
                    found.append(node)
                return
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)
        elif isinstance(node, str):
            # 有时候整段 aweme 是 JSON 字符串(嵌在别的字段里), 试着解一次
            t = node.strip()
            if t.startswith("{") and len(t) < 4 * 1024 * 1024:
                try:
                    walk(json.loads(t), depth + 1)
                except Exception:
                    pass

    walk(obj)
    return found


def collect_ids(obj: Any) -> list[str]:
    """从收藏动作的响应/请求体里找出 aweme_id。接口结构不统一, 广撒网。"""
    ids: list[str] = []

    def add(v):
        t = _norm_id(v)
        if not t:
            return
        # 纯数字短值(aweme_type=0 / type=0 / "123")是枚举噪音, 保留长度闸门;
        # 但非纯数字(将来 id 换成带前缀/哈希的字符串)一律收, 不再因长度被挡。
        if t.isdigit() and len(t) < 15:
            return
        if t not in ids:
            ids.append(t)

    def walk(node: Any, depth: int = 0):
        if depth > 10:
            return
        if isinstance(node, dict):
            for k in _ID_KEYS:
                if k in node:
                    add(node[k])
            for k in _ID_LIST_KEYS:
                v = node.get(k)
                if isinstance(v, list):
                    for x in v:
                        if isinstance(x, (str, int)) and not isinstance(x, bool):
                            add(x)
                        else:
                            walk(x, depth + 1)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)
        elif isinstance(node, str) and node.strip().startswith("{"):
            try:
                walk(json.loads(node), depth + 1)
            except Exception:
                pass

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
        if k.lower() in ("aweme_id", "awemeid", "item_id", "itemid",
                         "object_id", "group_id", "id"):
            t = _norm_id(v)
            if t and t not in ids:
                ids.append(t)
    return ids


def action_from_url(url: str) -> str:
    """从 query 里取 action 并归一化。取不到返回空串。"""
    try:
        qs = url.split("?", 1)[1] if "?" in url else ""
    except Exception:
        return ""
    for k, v in parse_qsl(qs, keep_blank_values=False):
        if k.lower() in ("action", "collect_action", "collectaction", "favorite_action"):
            a = _action_norm(v)
            if a:
                return a
    return ""


def action_value(url: str, body: Any = None, req: Any = None) -> str:
    """这次收藏接口调用到底在干什么, 返回 "1"/"0" 或空串。

    实测(2026-11-10, 用 data-e2e-state 对照验证过):
       action=1 -> 收藏; action=0 -> 取消收藏。别再按直觉反过来写。
    v1.6: 归一化布尔/字符串别名, 且 url 里没有就翻 req 再翻 body
    (收藏动作的参数更常在请求体里, 而不是响应体)。"""
    a = action_from_url(url or "")
    if a:
        return a
    for src in (req, body):
        if isinstance(src, str) and src.strip().startswith(("{", "[")):
            try:
                src = json.loads(src)
            except Exception:
                pass
        if isinstance(src, dict):
            for k in ("action", "collect_action", "collectAction", "favorite_action"):
                v = src.get(k)
                a = _action_norm(v)
                if a:
                    return a
    return ""


def is_uncollect(url: str, body: Any, req: Any) -> bool:
    """是不是「取消收藏」。只有明确带 action=0 才算; 没有 action 的同步请求不算取消。"""
    return action_value(url, body, req) == "0"


# ---------------------------------------------------------------------------
# v1.7 快手: 从 /rest/v/... 的响应/请求体里挖 photo
# ---------------------------------------------------------------------------

def looks_like_ks_photo(node: Any) -> bool:
    """一个对象像不像快手的 photo。"""
    node = node if isinstance(node, dict) else {}
    if not _is_ks_id(node.get("id") or node.get("photoId")):
        return False
    return any(k in node for k in _KS_VIDEO_KEYS)


def extract_ks_photos(obj: Any) -> list[dict]:
    """递归找出所有快手 photo 对象。

    快手的形状: {result, pcursor, feeds: [{photo: {...}, author: {...}}]}
    以及收藏列表：{feeds: [{photo: {...}}]} —— 结构一致, 都是往 feeds 里找。
    """
    found: list[dict] = []
    seen: set[int] = set()

    def walk(node: Any, depth: int = 0):
        if depth > 12:
            return
        if isinstance(node, dict):
            if looks_like_ks_photo(node):
                if id(node) not in seen:
                    seen.add(id(node))
                    found.append(node)
                return
            # v1.7 关键: 真实 REST feed 里 author 和 photo 是**平级**的,
            # 长这样  feeds[i] = {photo:{...}, author:{id,name,...}, ...}
            # 只取 photo 会丢作者名(文件名/面板都显示成"未知作者"), 这里把
            # 平级的 author 合进 photo 再缓存。Apollo 详情页里 author 本来
            # 就在 photo 内或由 _hl_fetch_ks 挂上, 不会重复。
            ph = node.get("photo")
            if isinstance(ph, dict) and looks_like_ks_photo(ph):
                merged = dict(ph)
                a = node.get("author")
                if isinstance(a, dict) and a and "author" not in merged:
                    merged["author"] = a
                if id(ph) not in seen:
                    seen.add(id(ph))
                    found.append(merged)
                # 别再遍历 node.values(), 免得把没合并 author 的 ph 又收一遍
                return
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)
        elif isinstance(node, str):
            t = node.strip()
            if t.startswith("{") and len(t) < 8 * 1024 * 1024:
                try:
                    walk(json.loads(t), depth + 1)
                except Exception:
                    pass

    walk(obj)
    return found


def collect_ks_ids(obj: Any) -> list[str]:
    """从快手收藏接口的请求/响应里找出 photoId。"""
    ids: list[str] = []

    def add(v):
        t = str(v or "").strip()
        if t and _is_ks_id(t) and t not in ids:
            ids.append(t)

    def walk(node: Any, depth: int = 0):
        if depth > 8:
            return
        if isinstance(node, dict):
            for k in ("photoId", "photo_id"):
                if k in node:
                    add(node.get(k))
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)
        elif isinstance(node, str) and node.strip().startswith("{"):
            try:
                walk(json.loads(node), depth + 1)
            except Exception:
                pass

    walk(obj)
    return ids


def ks_ids_from_url(url: str) -> list[str]:
    """快手详情页地址里的 photoId: /short-video/<id> 或 ?photoId=<id>。"""
    ids: list[str] = []
    try:
        m = re.search(r"/(?:short-video|photo|fw/photo)/([0-9A-Za-z_-]{8,32})",
                      url or "")
        if m and _is_ks_id(m.group(1)):
            ids.append(m.group(1))
        qs = (url or "").split("?", 1)[1] if "?" in (url or "") else ""
        for k, v in parse_qsl(qs, keep_blank_values=False):
            if k.lower() in ("photoid", "photo_id") and _is_ks_id(v):
                if v not in ids:
                    ids.append(v)
    except Exception:
        pass
    return ids


def ks_action_value(url: str, body: Any = None, req: Any = None) -> str:
    """快手这次 /rest/v/photo/collect 到底在干嘛, 返回 "1"(收藏)/"0"(取消)/""。

    实测(2026-11-13, 从 SwiperFeed-*.js 反混淆源码读出):
      `collect: photo.collected ? 2 : 1`  →  collect=1 收藏, collect=2 取消收藏
    注意这和抖音的 1/0 语义不同, 千万别照抄 _action_norm。
    """
    def pick(src) -> str:
        if isinstance(src, str) and src.strip().startswith("{"):
            try:
                src = json.loads(src)
            except Exception:
                return ""
        if not isinstance(src, dict):
            return ""
        for k in ("collect", "collectAction"):
            if k in src:
                try:
                    n = int(src.get(k))
                except Exception:
                    continue
                if n == 1:
                    return "1"
                if n == 2:
                    return "0"
        return ""

    for src in (req, body):
        a = pick(src)
        if a:
            return a
    # 兜底: 有些调用把 collect 放 query 里
    try:
        qs = (url or "").split("?", 1)[1] if "?" in (url or "") else ""
        for k, v in parse_qsl(qs, keep_blank_values=False):
            if k.lower() == "collect":
                n = int(v)
                return "1" if n == 1 else ("0" if n == 2 else "")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Chrome 管理
# ---------------------------------------------------------------------------

def find_chrome() -> str | None:
    """找 Chrome / Edge 可执行文件。

    v1.6: 以前只查 CHROME_PATH + 4 个写死的安装路径。用户把浏览器装在
    D 盘、用 Chromium、或 Win11 改了默认位置就找不到, 直接"启动不了"。
    现在补上注册表 App Paths、%LOCALAPPDATA%、PATH(shutil.which),
    并接受更多常见的可执行名。"""
    import shutil

    names = ["chrome.exe", "msedge.exe", "chromium.exe", "chrome-beta.exe"]
    cands: list[str] = []
    env = os.environ.get("CHROME_PATH")
    if env:
        cands.append(env)
    # 用户级安装(Chrome/Edge 都支持 per-user install)
    la = os.environ.get("LOCALAPPDATA") or ""
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    for base, sub in (
        (la, r"Google\Chrome\Application\chrome.exe"),
        (la, r"Microsoft\Edge\Application\msedge.exe"),
        (la, r"Chromium\Application\chrome.exe"),
        (pf, r"Google\Chrome\Application\chrome.exe"),
        (pf86, r"Google\Chrome\Application\chrome.exe"),
        (pf, r"Microsoft\Edge\Application\msedge.exe"),
        (pf86, r"Microsoft\Edge\Application\msedge.exe"),
        (pf, r"Chromium\Application\chrome.exe"),
    ):
        if base:
            cands.append(str(Path(base) / sub))
    # 注册表里登记的 App Paths(安装程序一定会写)。非 Windows 没有 winreg,
    # 整段包 try, 缺了就当没有这一路。
    try:
        import winreg
        for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"):
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        if val:
                            cands.append(str(val).strip('"'))
                except OSError:
                    continue
    except Exception:
        pass
    # 最后再扫一遍 PATH
    for n in names:
        try:
            got = shutil.which(n)
            if got:
                cands.append(got)
        except Exception:
            pass
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
        on_pending: Callable[[str], None] | None = None,
    ):
        self.profile_dir = Path(profile_dir)
        self.port = port
        self.on_aweme = on_aweme or (lambda a, s: None)
        self.on_status = on_status or (lambda kw: None)
        # (aweme_id, 原因) -> 报给面板失败列表; 主程序会注入
        self.on_fail: Callable[[str, str], None] | None = None
        # aweme_id -> 面板立刻显示"这条在解析中"。
        # v1.6.2: 以前要等无头浏览器把详情抓回来(5~12 秒)才第一次 publish,
        # 用户点完收藏盯着面板半天不动, 以为没生效。现在识别到收藏动作的
        # 那一刻就先占个位, 详情回来后由 add_waiting/begin 自动接管。
        self.on_pending = on_pending or (lambda aid: None)
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
        # v1.4: 自动下推荐页(默认关)。开启后 feed/related 流量里出现的视频
        # 全部进下载队列(去重), 不只当前刷到的那一条。
        self.auto_recommend = False
        self._auto_submitted: set[str] = set()   # 本会话已提交过的推荐视频, 防重复
        # 自动翻页间隔(秒)。太小会滑过头来不及下载, 太大会觉得"不动"。
        # 面板设置里可调, 范围 1~60。
        # v1.5: 自动替用户翻推荐页(默认开, 但要 auto_recommend 也开着才动作)。
        # 关掉它就只是"推荐页流量里出现什么就下什么", 不主动滑页面。
        self.auto_advance = True
        self.auto_advance_gap = 3.0
        # v1.6: 不能用 0.0 —— monotonic() 是"开机至今的秒数"(通常是几十万),
        # 0.0 会让第一次进主循环就满足 now-0>=gap 立刻翻页, 用户刚打开、
        # 还没看到第一条就被动跳过。初始化成 monotonic(), 先给用户一整个 gap
        # 的时间看第一条(首屏 feed 请求本来就会被钩子抓住并下载)。
        self._last_advance = time.monotonic()
        # 队列深度探针(主程序注入 lambda: dl.q.qsize())。
        # 翻页节奏可以比下载快很多 —— 每翻一次能塞进来 6 个视频,
        # 而抓详情+下载一个要几秒。积压太多就先停翻, 免得队列越滚越长。
        self.queue_probe = None
        self.auto_queue_high = 12      # 积压超过这个数就暂停翻页
        self._pause_noted = False
        self._lock = threading.Lock()
        self._pending_actions: list[str] = []
        # v1.7.1: 坏页面拉黑表 {url: 解禁时间戳} + 它的锁
        self._bad_pages: dict[str, float] = {}
        self._hook_lock = threading.Lock()
        # v1.7.2: raw CDP binding 回传通道。快手 new-reco 页在 Playwright 侧会
        # 变「半死页」(page.url 空、page.evaluate 永久挂), expose_binding 的
        # __dyReport 通道也断; 但 raw CDP Runtime.addBinding 走独立 websocket,
        # 不经过 Playwright attach, 对半死页依然有效。这里存 {targetId: worker 线程}。
        self._raw_lock = threading.Lock()
        self._raw_workers: dict[str, threading.Thread] = {}
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

    # -- v1.7 快手 ----------------------------------------------------------
    @staticmethod
    def _is_ks_url(url: str) -> bool:
        """这条流量是不是快手的。

        用域名 + 接口前缀双判定: 详情页/推荐页的 href 带 kuaishou.com,
        而 XHR 里的 url 有时是相对路径 `/rest/v/...`, 不带域名。
        """
        u = (url or "").lower()
        return "kuaishou.com" in u or "gifshow.com" in u or "/rest/v/" in u

    def _on_ks_payload(self, kind: str, url: str, body: Any, req: Any,
                       body_text: str, req_text: str):
        """快手事件独立分支。

        和抖音最本质的区别: 快手 CDN 直链**不过期、不需要 Cookie**
        (实测几十分钟前抓到的地址仍能裸请求下完 23MB), 所以收藏时
        **直接用 feed 响应里缓存的那份 photo 就行**, 不用像抖音那样
        每次都重新打开详情页抓签名地址。这样既快又完全绕开快手
        详情页那套「浏览器版本过低」风控。
        """
        if kind == "click":
            # 诊断用, 和抖音 click 分支保持同样的可读性
            try:
                info = json.loads(body_text) if (body_text or "").strip().startswith("{") else {}
            except Exception:
                info = {}
            self.log(f"  · 检测到快手收藏点击 [{info.get('mark') or '?'}] "
                     f"视频={info.get('id') or '未知'}")
            return

        if kind == "data":
            photos = extract_ks_photos(body)
            if not photos:
                return
            wrapped: list[dict] = []
            for ph in photos:
                pid = str(ph.get("id") or ph.get("photoId") or "")
                if not _is_ks_id(pid):
                    continue
                wrapped.append({
                    "aweme_id": pid,
                    "_platform": PLATFORM_KUAISHOU,
                    "_ks": ph,
                })
            if not wrapped:
                return
            self._remember(wrapped)

            if "/rest/v/collect/list" in url:
                # 和抖音 listcollection 同规矩: 收藏列表只用来建基线/缓存
                # 候选数据, 绝不作为下载触发(第一页内容会自己轮换, 靠列表
                # 比对会把旧收藏当成新收藏重下)。
                with self._lock:
                    fresh_baseline = not self.baseline_done
                    self.baseline_done = True
                    self.known_favs.update(w["aweme_id"] for w in wrapped)
                    if len(self.known_favs) > _SET_CAP_FAVS:
                        _cap_set(self.known_favs, _SET_CAP_FAVS)
                if fresh_baseline and self.download_all_on_start:
                    self.log(f"  快手收藏基线: {len(wrapped)} 个, 开始回补下载")
                    for w in wrapped:
                        self.on_aweme(w, "快手收藏列表")
                elif fresh_baseline:
                    self.log(f"  快手收藏基线: 已记录 {len(wrapped)} 个已有收藏(只下新的)")
                return

            # 自动下推荐页: 和抖音同一开关, 三道防线去重
            if self.auto_recommend and any(
                k in url for k in ("/rest/v/feed/", "/rest/v/profile/feed",
                                   "/rest/v/search/feed")
            ):
                todo: list[dict] = []
                with self._lock:
                    for w in wrapped:
                        aid = w["aweme_id"]
                        if aid in self._auto_submitted or aid in self.known_favs:
                            continue
                        self._auto_submitted.add(aid)
                        todo.append(w)
                    if len(self._auto_submitted) > _SET_CAP_AUTO:
                        _cap_set(self._auto_submitted, _SET_CAP_AUTO)
                if todo:
                    self.log(f"  快手推荐页: 新增 {len(todo)} 个视频进队列 "
                             f"(累计提交 {len(self._auto_submitted)})")
                    for w in todo:
                        try:
                            # 「快手推荐」不是「推荐页」: Downloader.process 只
                            # 对后者做详情页重抓, 快手直链不过期, 无需重抓。
                            self.on_aweme(w, "快手推荐")
                        except Exception as e:
                            self.log(f"  ! 快手推荐提交失败 {w['aweme_id']}: {e}")
                            with self._lock:
                                self._auto_submitted.discard(w["aweme_id"])
            return

        if kind == "action":
            act = ks_action_value(url, body, req)
            ids = (collect_ks_ids(req) or ks_ids_from_url(url)
                   or collect_ks_ids(body)[:1])
            self.log(f"  · 快手收藏接口 collect={act or '?'} "
                     f"req={req_text[:120] if req_text else '(空)'}")
            if not ids:
                return
            if act == "0":
                self.log(f"  · 取消收藏 {ids[0]} (不下载)")
                with self._lock:
                    self._pending_actions = [a for a in self._pending_actions
                                             if a != ids[0]]
                return
            if act != "1":
                self.log(f"  · 忽略快手状态同步请求 {ids[0]} (没有 collect)")
                return
            for aid in ids[:8]:
                cached = self._get_cached(aid)
                if cached:
                    # 命中 feed 缓存 → 立刻提交, 面板几乎零延迟
                    self.log(f"  · 新收藏 {aid} (快手), 已拿到视频信息, 直接下载")
                    try:
                        self.on_aweme(cached, "快手收藏")
                    except Exception as e:
                        self.log(f"  ! 快手收藏提交失败 {aid}: {e}")
                    continue
                # 冷启动或从分享链接直接进详情页时可能没缓存,
                # 才走无头详情页兜底(可能被风控挡, 失败会明确报给面板)
                with self._lock:
                    if aid in self._pending_actions:
                        continue
                    self._pending_actions.append(aid)
                try:
                    self.on_pending(aid)
                except Exception:
                    pass
                self.log(f"  · 新收藏 {aid} (快手), 正在抓取最新视频信息…")
            return

    def _on_payload(self, kind: str, url: str, body_text: str,
                    req_text: str, meta: dict = None):
        body = self._parse_body(body_text)
        req = self._parse_body(req_text)

        # v1.7: 快手走完全独立的一套解析。两边接口形状毫无共性
        # (快手 /rest/v/..., 抖音 /aweme/v1/...), 硬塞进同一个分支只会互相干扰。
        if self._is_ks_url(url):
            self._on_ks_payload(kind, url, body, req, body_text, req_text)
            return

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
                    # v1.6: 长跑防内存膨胀。被砍掉的旧 id 再出现会被当"新",
                    # 但 Downloader.submit 按 .state.json 去重, 不会真的重下。
                    if len(self.known_favs) > _SET_CAP_FAVS:
                        _cap_set(self.known_favs, _SET_CAP_FAVS)
                        self.log(f"  · 收藏基线集合已满({_SET_CAP_FAVS}), 淘汰最旧一批")

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

            # v1.4: 自动下推荐页 —— feed(推荐信息流)/related(相关推荐)流量里
            # 加载到的视频全部进下载队列。去重靠三道防线:
            #   1) _auto_submitted 本会话不重复提交;
            #   2) known_favs 里已有的收藏不重复交(收藏那条路会自己下);
            #   3) Downloader.submit 里的 state 已下载检查兜底。
            elif self.auto_recommend and (
                "/tab/feed" in url or "/aweme/related" in url
            ):
                todo = []
                with self._lock:
                    for a in awemes:
                        aid = str(a.get("aweme_id") or "")
                        if not aid or aid in self._auto_submitted or aid in self.known_favs:
                            continue
                        self._auto_submitted.add(aid)
                        todo.append(aid)
                    # v1.6: 同上, 长跑防 _auto_submitted 无限涨
                    if len(self._auto_submitted) > _SET_CAP_AUTO:
                        _cap_set(self._auto_submitted, _SET_CAP_AUTO)
                if todo:
                    self.log(f"  推荐页: 新增 {len(todo)} 个视频进队列 "
                             f"(累计提交 {len(self._auto_submitted)})")
                    for aid in todo:
                        aw = self._get_cached(aid)
                        if aw:
                            try:
                                self.on_aweme(aw, "推荐页")
                            except Exception as e:
                                self.log(f"  ! 推荐页提交失败 {aid}: {e}")
                                # 提交失败时撤回标记, 下次流量再见到还能再试
                                with self._lock:
                                    self._auto_submitted.discard(aid)

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
                # 用户点完马上取消的场景: 把还没开始抓的收藏撤掉, 免得下完才发现不要
                try:
                    with self._lock:
                        self._pending_actions = [a for a in self._pending_actions if a != _u[0]]
                except Exception:
                    pass
                return
            # 请求体里带的是「这一次操作的那个视频」, 最准确;
            # 响应体里常常带着整个收藏列表, 拿来当目标会误下, 只在前者为空时兜底。
            ids = collect_ids(req) or ids_from_url(url) or collect_ids(body)[:1]
            if not ids:
                return
            # 只认明确带 action 的请求:
            #   实测页面自己发的状态同步请求不带 action(req 是 aweme_id=..&item_type=0&type=0),
            #   带 action=1 的一定是人点了收藏。
            # 早期用「必须匹配到点击记录」来挡同步请求, 结果连真实点击也一起挡了
            # (连点两个视频、或按钮在详情浮层里时, 点击记录对不上号) —— 已废弃。
            # v1.6: 这个判断对所有 id 都一样, 提到循环外只算一次, 也避免循环里
            # 因为 return 提前退出而漏掉同批次的其它视频。
            _act = action_value(url, body, req)
            if _act != "1":
                self.log(f"  · 忽略状态同步请求 {ids[0]} (没有 action)")
                return
            # 一律重新抓一次详情, 不吃缓存:
            # 缓存(尤其是收藏列表接口带来的)里的播放地址签名很快失效,
            # 直接拿去下载会整个视频所有候选都 403。实测重新抓详情后同一个视频立刻能下。
            # v1.6: 一次收藏通常只带一个 id; 极端情况下(批量请求)也只取前 8 个,
            # 免得一个异常请求把待补全队列刷爆。用 continue 语义逐个入队, 不再 ids[:1]。
            for aid in ids[:8]:
                with self._lock:
                    if aid not in self._pending_actions:
                        self._pending_actions.append(aid)
                    else:
                        continue          # 已经在补全队列里, 别重复告知面板
                # 先让面板上出现"解析中", 不用等详情抓回来
                try:
                    self.on_pending(aid)
                except Exception:
                    pass
                self.log(f"  · 新收藏 {aid}, 正在抓取最新视频信息…")

    # -- 补全: 用无头浏览器取详情 -------------------------------------------
    @staticmethod
    def _launch_headless(pw):
        """起一个无头 Chromium, 自动适配本机装了哪种浏览器。

        教训(v1.6.1): 之前写死不指定 channel, 以为"Playwright 自带 chromium
        已随 pip 装好"——错的, pip 只装了驱动库, 浏览器二进制要额外
        `playwright install` 才会下载, 没装的话每次都抛 "Executable
        doesn't exist", 所有收藏补全都拿不到详情, 一个都下不了。
        现在按"最可能成功"的顺序逐个尝试, 哪个能起用哪个:
          1. find_chrome() 找到的系统 Chrome/Edge(executable_path)
          2. channel="chrome"(系统标准安装的 Chrome)
          3. channel="msedge"(只有 Edge 的机器)
          4. Playwright 自带 chromium(用户跑过 playwright install 的情况)
        全失败才把最后一个异常抛给调用方记日志。
        """
        args = [
            "--disable-blink-features=AutomationControlled",
            "--mute-audio",
            "--disable-gpu",
            "--no-first-run",
        ]
        attempts: list[tuple[str, dict]] = []
        try:
            exe = find_chrome()
        except Exception:
            exe = None
        if exe:
            attempts.append((f"系统浏览器({Path(exe).name})", {"executable_path": exe}))
        attempts.append(('channel="chrome"', {"channel": "chrome"}))
        attempts.append(('channel="msedge"', {"channel": "msedge"}))
        attempts.append(("Playwright 自带 chromium", {}))
        last_err: Exception | None = None
        for label, kw in attempts:
            try:
                b = pw.chromium.launch(headless=True, args=args, **kw)
                return b
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(
            "无头浏览器起不来(系统 Chrome/Edge 与自带 chromium 都不可用): "
            + (str(last_err)[:160] if last_err else "未知原因")
        )

    def _hl_loop(self):
        """
        后台线程: 用一个无头 Chrome 打开视频页, 抓页面自己发出的详情接口。

        为什么要有这条线程: Playwright 的 sync API 必须待在创建它的线程里,
        所以这里单独 start() 一个 playwright 实例, 和主循环完全隔离。
        好处是整个过程没有任何窗口, 用户眼前绝对不会跳东西。
        """
        # v1.6: inflight 记录"正取到一半"的 id。若线程在取详情途中崩了,
        # 这个 id 已经从 _hl_in 拿出来、不在队列里, 光排空队列会漏掉它,
        # 主循环得白等 45 秒才超时重排。记下来, 崩溃时一并回 None。
        inflight: str | None = None
        try:
            with sync_playwright() as pw:
                browser = self._launch_headless(pw)
                ctx = browser.new_context(
                    user_agent=UA,
                    viewport={"width": 1280, "height": 800},
                    locale="zh-CN",
                )
                page = ctx.new_page()
                # v1.6.3: 这里曾经有一段「排空残留队列」的逻辑, 是个严重的竞态:
                # _resolve_pending 先 _ensure_hl_worker() 起线程, 然后才去取
                # cookies、把 (aid, cookies) 投进 _hl_in; 而线程要等
                # sync_playwright 启动 + _launch_headless 之后才跑到排空这段,
                # 那时新请求已经躺在队列里了 —— 被当成"残留"丢掉并回 None,
                # 主循环随即判定"拿不到视频信息"。表现就是: 每次启动后的第一个
                # 收藏必然失败、后面的都正常, 用户以为下载功能坏了。
                # 真正的需求只是别把上一次的退出信号(None)当成这次的退出信号,
                # 那件事放 _ensure_hl_worker 里做, 且只丢 None、真请求一律保留。
                while True:
                    item = self._hl_in.get()
                    if item is None:            # 退出信号
                        break
                    try:
                        aid, cookies = item
                    except Exception:
                        continue                 # 畸形条目, 跳过(不能让整个线程崩)
                    inflight = aid
                    aw = None
                    try:
                        aw = self._hl_fetch(page, ctx, aid, cookies)
                    except Exception as e:
                        self.log(f"  ! 无头取详情失败 {aid}: {str(e)[:60]}")
                        # 页面可能已被抖音跳转/崩溃弄死: 换个新的, 不然之后每次都失败
                        try:
                            if page.is_closed():
                                page = ctx.new_page()
                        except Exception:
                            try:
                                page = ctx.new_page()
                            except Exception:
                                break
                    self._hl_out.put((aid, aw))
                    inflight = None
                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            self.log(f"  ! 无头浏览器启动失败: {str(e)[:90]}")
            # 让所有排队的请求(含取到一半的)都拿到 None, 否则主循环会一直等。
            # v1.6: 旧写法 `aid, _ = self._hl_in.get_nowait()` 对 None 哨兵会
            # TypeError: cannot unpack non-sequence NoneType, 把兜底自己也搞崩。
            if inflight is not None:
                try:
                    self._hl_out.put((inflight, None))
                except Exception:
                    pass
            while True:
                try:
                    item = self._hl_in.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    break
                try:
                    self._hl_out.put((item[0], None))
                except Exception:
                    pass

    def _hl_fetch(self, page, ctx, aid, cookies):
        """在无头页面里打开视频, 等详情数据回来。

        v1.7: 抖音走 /aweme/detail/ 响应; 快手走 window.__APOLLO_STATE__。
        按 id 形状分派(抖音纯数字、快手字母数字混排), 不用额外传站点参数,
        队列里仍是原来的 (aid, cookies) 二元组。
        """
        try:
            ctx.clear_cookies()
            if cookies:
                ctx.add_cookies(cookies)
        except Exception:
            pass

        if _is_ks_id(aid):
            return self._hl_fetch_ks(page, aid)

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

    def _hl_fetch_ks(self, page, aid: str):
        """快手兜底: 打开详情页读 window.__APOLLO_STATE__ 并解引用。

        只在「feed 缓存里没这条」时才会走到这里(例如用户从分享链接
        直接进详情页点收藏)。快手详情页有版本/自动化风控, 可能只返回
        14 个配置键而没有 photo —— 那就返回 None, 上层会明确报失败,
        用户往下滑一下让 feed 重新加载再点一次收藏即可。

        Apollo 里全是 {"type":"id","id":"VisionVideoSetRepresentation:1"}
        这种引用, 必须在 JS 里递归解引用成普通对象再回传, 否则 Python
        拿到的只是个指针, 一个候选都解析不出来。
        """
        js = r"""
        (pid) => {
          const s = window.__APOLLO_STATE__;
          if (!s) return null;
          const C = s.defaultClient || s;
          const deref = (v, d) => {
            if (d > 8 || v === null || v === undefined) return v;
            if (typeof v !== "object") return v;
            if (Array.isArray(v)) return v.map(x => deref(x, d + 1));
            if (v.type === "json" && "json" in v) return deref(v.json, d + 1);
            if (v.type === "id" && typeof v.id === "string" && C[v.id])
              return deref(C[v.id], d + 1);
            const o = {};
            for (const k of Object.keys(v)) o[k] = deref(v[k], d + 1);
            return o;
          };
          const key = "VisionVideoDetailPhoto:" + pid;
          const raw = C[key];
          if (!raw) return null;
          const photo = deref(raw, 0);
          // 和 photo 同级的作者信息也在 client 里, 顺手挂上去
          for (const k of Object.keys(C)) {
            if (k.indexOf("VisionVideoDetailAuthor:") === 0) {
              photo.author = deref(C[k], 0);
              break;
            }
          }
          return photo;
        }
        """
        try:
            page.goto(f"https://www.kuaishou.com/short-video/{aid}",
                      wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            self.log(f"  ! 打开快手详情页失败 {aid}: {str(e)[:60]}")
            return None
        for _ in range(20):                     # 最多等 10 秒
            try:
                photo = page.evaluate(js, aid)
            except Exception:
                photo = None
            if isinstance(photo, dict) and (photo.get("manifest")
                                            or photo.get("photoUrl")):
                info = parse_kuaishou(photo)
                if info.candidates:
                    self.log(f"  · 快手详情页拿到了 {aid} 的 "
                             f"{len(info.candidates)} 个画质候选")
                    return {"aweme_id": aid, "_platform": PLATFORM_KUAISHOU,
                            "_ks": photo}
                return None
            page.wait_for_timeout(500)
        self.log(f"  ! 快手详情页没给视频数据 {aid} "
                 "(被风控挡了, 往下滑一下让 feed 重新加载再收藏一次即可)")
        return None

    def _ensure_hl_worker(self):
        if self._hl_thread is not None and self._hl_thread.is_alive():
            return
        # v1.6: 两条队列只建一次, 线程重启时复用。旧写法每次都 new 两个 Queue,
        # 上一个线程崩了/退出时残留的 _hl_in 请求和 _hl_out 结果全成了孤儿 ——
        # 主循环还在 _resolving 里等那些 id, 结果永远等不到, 白等 45 秒超时。
        # 复用后新线程的"排空残留"逻辑会给每个残留 id 回一个 None, 主循环立刻重排。
        if self._hl_in is None:
            self._hl_in = queue.Queue()
        if self._hl_out is None:
            self._hl_out = queue.Queue()
        # v1.6.3: 在**主循环投递新请求之前**清掉上次留下的退出信号。
        # 只丢 None, 真正的 (aid, cookies) 一个都不动 —— 上一版在线程里排空,
        # 而那时新请求已经入队, 于是每次启动后第一个收藏都被当成残留吃掉。
        try:
            kept: list = []
            while True:
                try:
                    it = self._hl_in.get_nowait()
                except queue.Empty:
                    break
                if it is not None:
                    kept.append(it)
            for it in kept:
                self._hl_in.put(it)
        except Exception:
            pass
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
            # v1.6: CDP 返回的是外部数据, 别裸下标。旧版任意一个字段缺失
            # (新版 Chrome 改字段、返回 None、targetInfos 不是 list)就 KeyError/
            # TypeError 直接把这轮清理整个废掉, 还可能让残留窗口越积越多。
            targets = (cdp.send("Target.getTargets") or {}).get("targetInfos")
            if not isinstance(targets, list):
                return
            for t in targets:
                if not isinstance(t, dict) or t.get("type") != "page":
                    continue
                tid = t.get("targetId")
                if not isinstance(tid, str) or not tid:
                    continue
                try:
                    w = cdp.send("Browser.getWindowForTarget",
                                 {"targetId": tid}) or {}
                    wid = w.get("windowId")
                    if wid is None:
                        continue
                    bd = (cdp.send("Browser.getWindowBounds",
                                   {"windowId": wid}) or {}).get("bounds") or {}
                    if not isinstance(bd, dict):
                        continue
                    left = int(bd.get("left") or 0)
                except Exception:
                    continue
                if left <= -2000:
                    found.append(tid)
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
        保证浏览器里有一个能刷的站点页面(抖音或快手都算)。

        踩过的坑: 为了"不打扰用户"把启动时的 goto 删了, 结果冷启动后窗口里只有
        一个 about:blank —— 用户看到的是白屏, 以为浏览器坏了。
        这里的规矩: 已经有站点页就一个字都不动; 全都没有才自己开一个。

        v1.7: 只认抖音会把「只开了快手页」误判成没页面, 于是每 60 秒强开
        一个抖音标签页、把用户正在刷的快手挤到后台。两个站点一视同仁。
        """
        for pg in list(ctx.pages):
            try:
                if any(d in (pg.url or "") for d in _SITE_DOMAINS):
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
            # v1.6: 详情一直抓不到时 _pending_actions 会被反复放回、越堆越多,
            # 长跑吃内存。超过上限只留最近的一批(老的就算放弃, 面板会记失败)。
            if len(pend) > _SET_CAP_RESOLVING:
                self.log(f"  · 待补全队列过长({len(pend)}), 丢弃最旧的 "
                         f"{len(pend) - _SET_CAP_RESOLVING} 个")
                pend = pend[-_SET_CAP_RESOLVING:]

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

        cookies = self._douyin_cookies() + self._ks_cookies()
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
                    # 必须报到面板: 否则用户点了收藏什么都没发生, 也不知道为什么
                    if callable(getattr(self, "on_fail", None)):
                        try:
                            self.on_fail(aid, "抓不到视频详情(可能已删除/私密/需要登录)")
                        except Exception:
                            pass

        if self._resolving:                      # 超时: 放回队列下轮再来
            for aid in list(self._resolving):
                self._resolving.discard(aid)
                with self._lock:
                    self._pending_actions.append(aid)

    def _douyin_cookies(self):
        """把已登录浏览器里的抖音 Cookie 抠出来, 给无头浏览器用。"""
        return self._cookies_for("douyin")

    def _ks_cookies(self):
        """快手 Cookie(游客态也有一组, 详情页风控可能会看)。"""
        return self._cookies_for("kuaishou")

    def _cookies_for(self, domain_key: str) -> list:
        out = []
        try:
            for c in self._ctx.cookies():
                if domain_key not in (c.get("domain") or ""):
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
                info = json.loads(r.read())
                # v1.6: 端口上可能不是 Chrome(别的程序占了、返回了个 JSON 数组
                # 或 null), 旧版直接 return 非 dict -> start_chrome 里 info.get
                # AttributeError。只认 dict, 且必须带 Browser/webSocketDebuggerUrl
                # 才算"可用的 CDP 浏览器"。
                if not isinstance(info, dict):
                    return None
                if not (info.get("Browser") or info.get("webSocketDebuggerUrl")):
                    return None
                return info
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
                if not isinstance(info, dict):
                    raise ValueError("CDP 返回的不是对象")
                self._browser_name = info.get("Browser", "") or ""
                self.log(f"  浏览器已就绪: {self._browser_name}")
                return True
            except Exception:
                if self.proc.poll() is not None:
                    self.log("! 浏览器启动后立即退出(可能端口被占用)")
                    return False
                time.sleep(0.5)
        self.log("! 等待调试端口超时")
        return False

    # 只列"登录了才会有"的 Cookie。ttwid / passport_csrf_token / msToken /
    # __ac_signature 游客态也有(实测游客 profile 里就这几个), 绝不能拿来判登录,
    # 否则会把未登录当已登录, 后面所有请求全是 403 还一头雾水。
    _LOGIN_COOKIES = frozenset({
        "sessionid", "sessionid_ss", "sid_tt", "sid_guard",
        "uid_tt", "uid_tt_ss",
    })

    # 快手游客态也有 kwssectoken/kwscode/kwfv1 这几个(实测 9 个游客 cookie),
    # 所以**绝不能**拿它们当登录标志 —— 会把游客态误判成已登录。
    # 快手这边根本不需要判登录: 收藏动作是用户浏览器自己发的(没登录就点不了),
    # 而 CDN 直链是公开的、下载不要 Cookie。所以只要浏览器里确实开着快手页,
    # 就认为「至少快手这条路可用」, 别让只用快手的用户干等 10 分钟抖音登录墙。
    _KS_PAGE_DOMAINS = ("kuaishou.com",)

    @classmethod
    def check_login(cls, ctx) -> bool:
        try:
            # v1.6: 旧版只认 3 个名字, 抖音改过登录态字段名(sid_guard/uid_tt
            # 现在也会带), 只守着老三样会偶发"明明登录了却一直让我扫码"。
            # 名字命中还不够, 值必须非空(有些 Cookie 退出登录后名字还在、值清空)。
            for c in ctx.cookies():
                if c.get("name") in cls._LOGIN_COOKIES and c.get("value"):
                    return True
        except Exception:
            pass
        # v1.7: 抖音没登录、但用户确实开着快手页 → 让监听继续跑。
        # 抖音那条路此时会 403 并明确报失败, 但快手收藏照常能下。
        try:
            for pg in ctx.pages:
                if any(d in (pg.url or "") for d in cls._KS_PAGE_DOMAINS):
                    return True
        except Exception:
            pass
        return False

    # -- 主循环 ------------------------------------------------------------
    def run(self):
        """跑监听主循环。返回 0 正常结束; 7 = 用户主动关闭(守护看到 7 不再拉起)。

        v1.6: 所有失败出口必须返回明确的非零码。旧版这几处是裸 `return`
        (返回 None), 在 `sys.exit(main())` 里 None 会被当 0 —— 浏览器起不来
        这种真故障, 守护却以为"正常退出", 日志里看不到异常, 排障被带偏。
        """
        if not self.start_chrome():
            return EXIT_BROWSER_FAIL
        self._intentional_exit = False

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
                self._hook_all_pages(ctx)

                # 冷启动时窗口里可能只有一个 about:blank, 自动补一个抖音页
                page = self._ensure_douyin_tab(ctx) or page
                self._page = page

                if not self.check_login(ctx):
                    self.log("")
                    self.log("  ┌─────────────────────────────────────────────┐")
                    self.log("  │  请在弹出的浏览器窗口里登录抖音或快手        │")
                    self.log("  │  登录后会自动继续, 无需其他操作              │")
                    self.log("  └─────────────────────────────────────────────┘")
                    self.log("")
                    deadline = time.time() + 600
                    while time.time() < deadline:
                        time.sleep(2)
                        # Chrome 起来又死了就别干等 10 分钟: 退出让守护重新走一遍流程
                        if (getattr(self, "_browser", None) is not None
                                and not self._browser.is_connected()):
                            self.log("! 浏览器在登录等待时关闭了, 监听结束")
                            return EXIT_BROWSER_FAIL
                        self.pump(ctx)
                        if self.check_login(ctx):
                            break
                    if not self.check_login(ctx):
                        self.log("! 登录等待超时, 退出")
                        return EXIT_LOGIN_TIMEOUT
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
                self.log("    现在去抖音 / 快手正常刷、点『收藏』, "
                         "视频会自动下载到 downloads/")
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
                        if (getattr(self, "_browser", None) is not None
                                and not self._browser.is_connected()):
                            # 关掉最后一个抖音 tab 后 Chrome 窗口会消失, is_connected 随之变 False
                            # —— 这才是用户"关掉抖音"的真实路径(而不是下面的 tick%150 分支)。
                            # auto_reopen=关: 视为用户主动关闭, 用退出码 7 告诉守护别再拉起。
                            try:
                                from douyin_dl.ui import load_settings
                                _sr = load_settings(Path(__file__).resolve().parent.parent / ".settings.json").get("auto_reopen", True)
                            except Exception:
                                _sr = True
                            if not _sr:
                                self.log("  自动重开已关闭, 抖音已关闭, 监听结束")
                                self._intentional_exit = True
                            else:
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
                    # v1.5: auto_recommend 开着时自动翻推荐页。
                    # 抖音 web 在"停留看当前视频"时不发新 feed 请求, 只有滑到
                    # 下一条才发一次 /tab/feed(6 条) —— 用户不滑就永远没流量,
                    # 造成"点一下收藏才下一批"的错觉。这里按固定间隔替用户翻页,
                    # 让推荐流持续产出。用户正在别的页面(搜索/个人主页/详情)时不打扰。
                    now = time.monotonic()
                    if (self.auto_recommend and self.auto_advance
                            and (now - self._last_advance) >= self.auto_advance_gap):
                        self._last_advance = now
                        self._auto_advance_feed(ctx)
                    if tick % 15 == 0:             # 每 ~6 秒巡检一次钩子是否还在
                        self._hook_all_pages(ctx)
                        self._sync_cookies(ctx)
                    if tick % 150 == 0:            # 每 ~60 秒: 窗口被关空了看情况补一个
                        try:
                            # 不用 ctx.pages —— 页面全关后 pump 不再通信, 销毁事件永远收不到,
                            # pages 列表就是死的。直接问 CDP 要真实 target 列表(HTTP 一次往返)。
                            import urllib.request as _ur, json as _json
                            try:
                                _tabs = _json.loads(_ur.urlopen(
                                    f"http://127.0.0.1:{self.port}/json/list", timeout=5).read())
                                _has_dy = any(
                                    any(d in t.get("url", "") for d in _SITE_DOMAINS)
                                    for t in _tabs if t.get("type") == "page")
                            except Exception:
                                _has_dy = bool([x for x in ctx.pages if not x.is_closed()])
                            if not _has_dy:
                                # 用户可关掉自动重开: 面板设置里"退出抖音后不再自动打开"
                                try:
                                    from douyin_dl.ui import load_settings
                                    _sr = load_settings(Path(__file__).resolve().parent.parent / ".settings.json").get("auto_reopen", True)
                                except Exception:
                                    _sr = True
                                if not _sr:
                                    self.log("  自动重开已关闭, 抖音已全部关闭, 监听结束")
                                    self._intentional_exit = True
                                    break
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
        return 7 if getattr(self, "_intentional_exit", False) else 0

    def _auto_advance_feed(self, ctx):
        """
        自动翻一条推荐视频(按 ↓), 让页面发新的 /tab/feed 请求。
        只动推荐页 tab; 用户在其他页面(搜索/个人主页/详情)时不动,
        免得打扰操作。队列里积压太多时也先不翻, 给下载留时间。
        """
        try:
            # 开关自己也要守一道: 面板刚关掉时, 主循环可能还差一拍才停下来
            if not (self.auto_recommend and self.auto_advance):
                return
            # 背压: 下载队列积压太多就先不翻, 等消化一些再继续。
            # 不这么做的话 3 秒一翻 ≈ 每秒 2 个视频入队, 永远追不上。
            try:
                depth = int(self.queue_probe()) if callable(self.queue_probe) else 0
            except Exception:
                depth = 0
            if depth >= self.auto_queue_high:
                if not self._pause_noted:
                    self.log(f"  · 队列积压 {depth} 个, 先暂停翻页, 消化完自动继续")
                    self._pause_noted = True
                return
            if self._pause_noted:
                self.log(f"  · 队列已消化到 {depth} 个, 继续翻页")
                self._pause_noted = False
            for pg in list(ctx.pages):
                try:
                    if pg.is_closed():
                        continue
                    u = pg.url or ""
                    # v1.7 快手推荐页: /new-reco 是全屏 Swiper, 按 ↓ 切下一条,
                    # 页面自己会发 /rest/v/feed/hot 请求, 钩子照常搭便车。
                    if "kuaishou.com" in u:
                        if not ("/new-reco" in u or "/reco" in u
                                or u.rstrip("/").count("/") <= 2):
                            continue
                        pg.keyboard.press("ArrowDown")
                        self.log("  · 自动翻页: 快手推荐流下一条")
                        return  # 一轮只翻一次
                    if "douyin.com" not in u:
                        continue
                    # 只翻推荐页(推荐 tab 的 url 是 /?recommend=1 或 /recommend 或根路径的推荐流)
                    if not ("/recommend" in u or "recommend=1" in u or
                            (u.rstrip("/").count("/") <= 2 and "video/" not in u
                             and "search" not in u and "/user/" not in u
                             and "follow" not in u and "hot" not in u)):
                        continue
                    # 页面失焦与否无所谓, 按键走 CDP 不需要真前台
                    pg.keyboard.press("ArrowDown")
                    self.log("  · 自动翻页: 推荐流下一条")
                    return  # 一轮只翻一次
                except Exception:
                    continue
        except Exception:
            pass

    def _sync_cookies(self, ctx):
        """
        把浏览器里的登录 Cookie 同步给下载器。
        抖音的视频地址带 tk=webid, 是绑定登录态的; 不带 Cookie 直接 GET 会 403。

        v1.7: 这里**只同步抖音 cookie**。快手 CDN 直链是公开的
        (实测裸请求就能 200 下完 23MB), 不需要任何 Cookie; 把快手那堆
        kwpsecproductname/kwssectoken 混进抖音的 Cookie 头反而可能让
        抖音 CDN 挑刺, 所以刻意不合并。快手登录态只用于「点收藏」这个
        页面动作, 那是用户浏览器自己的事。
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

        v1.7.1: pages[0] 可能是坏页面(渲染进程死), wait_for_timeout 同样会被
        拖住。优先挑一个健康的站点页来 pump; 没有就退回 browser 级 CDP
        (Browser.getVersion 是一次和浏览器进程的轻往返, 不碰任何页面渲染进程)。
        """
        try:
            for pg in ctx.pages:
                try:
                    u = pg.url or ""
                except Exception:
                    u = ""
                if any(d in u for d in _SITE_DOMAINS) and not self._page_blacklisted(u):
                    pg.wait_for_timeout(1)
                    return
            # 没有健康的站点页: 用 browser 级 CDP 做安全往返
            try:
                cdp = self._browser.new_browser_cdp_session()
                try:
                    cdp.send("Browser.getVersion")
                finally:
                    try:
                        cdp.detach()
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

    # ---- v1.7.1: 页面健康度看门狗 ----------------------------------
    # Page.evaluate() 在 Playwright 里 **没有 timeout 形参**, 对一个已经挂掉/
    # 还没初始化好的渲染进程会一直阻塞(实测 68 秒才抛 Connection closed, 而
    # 主循环就卡在这一行 —— 整个监听停摆, 抖音和快手的事件全都收不到)。
    # 所以: ① 只给真正的站点页装钩子, 空 URL / about: / 别的网站一律跳过;
    #        ② 每次巡检验页面之前先用 CDP 的 Runtime.evaluate 带硬超时探活,
    #           探不通就拉黑一段时间, 绝不对它调 page.evaluate。
    _PROBE_TIMEOUT = 4.0        # 单次探活最多等多久(秒)
    _BLACKLIST_SECS = 120.0     # 探活失败后拉黑多久

    def _page_url(self, page):
        """拿页面 URL, 拿不到就返回空串(某些坏页连 .url 都会抛)。

        v1.7.2: Playwright 侧 page.url 为空但页面其实还活着的「半死页」
        (典型是快手 new-reco 页)会在这里漏判成非站点页, 导致钩子装不上、
        收藏事件全部丢失。所以 page.url 为空时用 CDP Target.getTargetInfo
        再问一次真实 URL。
        """
        try:
            url = page.url or ""
        except Exception:
            url = ""
        if url:
            return url
        # page.url 空: 可能是半死页。CDP 层通常还活着, 拿真实 URL。
        tid = self._page_debugger_id(page)
        if not tid:
            return ""
        try:
            tabs = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/list",
                timeout=self._PROBE_TIMEOUT).read())
            for t in tabs:
                if isinstance(t, dict) and t.get("id") == tid:
                    return t.get("url") or ""
        except Exception:
            return ""
        return ""

    def _is_site_url(self, url):
        """只有真正的抖音/快手页面才值得装钩子。"""
        return any(d in url for d in _SITE_DOMAINS)

    def _page_alive(self, page):
        """用 CDP 的 Runtime.evaluate 探活(自带超时), 不用 page.evaluate。

        走 websocket 直连 CDP: `websocket-client` 的 recv 支持 timeout, 探不通
        就在 _PROBE_TIMEOUT 秒内返回 False, 不会把调用方拖住。探针脚本里就是
        用这个方式量出「坏页面 68 秒不响应」的。
        """
        tid = None
        try:
            tid = self._page_debugger_id(page)
        except Exception:
            return False
        if not tid:
            return False
        return self._cdp_probe(tid)

    def _cdp_probe(self, tid):
        """按 targetId 用 CDP Runtime.evaluate 探活(自带超时), 不依赖 page 对象。

        供「Playwright 看不见的孤儿页」补漏时用 —— 这类页面没有 page 对象,
        `_page_alive(page)` 无从调用, 只能按 tid 直连 CDP 探一次。
        """
        try:
            import websocket as _ws
        except Exception:
            return True
        try:
            tabs = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/list", timeout=self._PROBE_TIMEOUT).read())
            ws = next((t.get("webSocketDebuggerUrl") for t in tabs
                       if isinstance(t, dict) and t.get("id") == tid), None)
            if not ws:
                return False
            conn = _ws.create_connection(ws, timeout=self._PROBE_TIMEOUT,
                                         enable_multithread=True)
            try:
                conn.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                      "params": {"expression": "1", "returnByValue": True}}))
                deadline = time.time() + self._PROBE_TIMEOUT
                while time.time() < deadline:
                    msg = json.loads(conn.recv())
                    if msg.get("id") == 1:
                        return "result" in msg
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            return False
        return False

    def _page_debugger_id(self, page):
        """用 Playwright 的私有 CDP 会话拿 targetId; 拿不到返回 None。

        这里只读属性, 不发网络请求, 不会阻塞。
        """
        try:
            ctx = getattr(self, "_ctx", None)
            if ctx is None:
                return None
            cdp = ctx.new_cdp_session(page)
            try:
                info = cdp.send("Target.getTargetInfo")
                return ((info or {}).get("targetInfo") or {}).get("targetId")
            finally:
                try:
                    cdp.detach()
                except Exception:
                    pass
        except Exception:
            return None

    def _raw_cdp(self, tid, method, params=None, timeout=6.0):
        """对某个 target 做一次独立 CDP 往返(不走 Playwright attach)。"""
        try:
            import websocket as _ws
        except Exception:
            return None
        try:
            tabs = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/list",
                timeout=self._PROBE_TIMEOUT).read())
            ws_url = next((t.get("webSocketDebuggerUrl") for t in tabs
                           if isinstance(t, dict) and t.get("id") == tid), None)
            if not ws_url:
                return None
            conn = _ws.create_connection(ws_url, timeout=timeout,
                                         enable_multithread=True)
            try:
                conn.send(json.dumps({"id": 1, "method": method,
                                      "params": params or {}}))
                deadline = time.time() + timeout
                while time.time() < deadline:
                    msg = json.loads(conn.recv())
                    if msg.get("id") == 1:
                        return msg.get("result")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            return None
        return None

    def _raw_binding_worker(self, tid: str):
        """给一个 target 开一条独立的 raw CDP binding 通道。

        用 websocket 直连 CDP, Runtime.addBinding 注册 __dyReportRaw,
        收到 Runtime.bindingCalled 就把 payload 当 binding 事件入队
        (和 expose_binding 的 _on_binding 走同一条 queue, 主循环 drain 处理)。
        这条通道不经过 Playwright 的 attach, 所以对「半死页」(page.evaluate
        挂起、expose_binding 断)依然有效。
        """
        try:
            import websocket as _ws
        except Exception:
            return
        ws_url = None
        try:
            tabs = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/list",
                timeout=self._PROBE_TIMEOUT).read())
            ws_url = next((t.get("webSocketDebuggerUrl") for t in tabs
                           if isinstance(t, dict) and t.get("id") == tid), None)
        except Exception:
            ws_url = None
        if not ws_url:
            return
        conn = None
        try:
            conn = _ws.create_connection(ws_url, timeout=self._PROBE_TIMEOUT,
                                         enable_multithread=True)
            conn.send(json.dumps({"id": 1, "method": "Runtime.enable",
                                  "params": {}}))
            conn.send(json.dumps({"id": 2, "method": "Runtime.addBinding",
                                  "params": {"name": "__dyReportRaw"}}))
            # 先收掉两个 id 的响应, 之后就是 bindingCalled 事件流
            got = set()
            deadline = time.time() + self._PROBE_TIMEOUT
            while time.time() < deadline:
                msg = json.loads(conn.recv())
                if msg.get("id") in (1, 2):
                    got.add(msg.get("id"))
                if msg.get("method") == "Runtime.bindingCalled":
                    name = (msg.get("params") or {}).get("name")
                    if name == "__dyReportRaw":
                        self._on_binding("__dyReportRaw",
                                         (msg.get("params") or {}).get("payload") or "")
                if 1 in got and 2 in got:
                    break
            # 握手阶段靠 recv 的 4s 超时 + deadline 兜底; 握手完成后必须把
            # socket 超时关掉, 否则空闲 4 秒 recv() 就抛 WebSocketTimeoutException
            # 被外层 except 吞掉 -> 线程 4 秒后静默死亡 -> raw 通道断开(事件丢失)。
            # 建连成功后 recv() 应无限阻塞, 只靠 _hook_all_pages 每 6 秒巡检重建。
            try:
                conn.settimeout(None)
            except Exception:
                pass
            # 事件循环
            while True:
                msg = json.loads(conn.recv())
                if msg.get("method") == "Runtime.bindingCalled":
                    name = (msg.get("params") or {}).get("name")
                    if name == "__dyReportRaw":
                        self._on_binding("__dyReportRaw",
                                         (msg.get("params") or {}).get("payload") or "")
        except Exception:
            pass
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            # 线程退出时把自己从注册表摘掉
            with self._raw_lock:
                if self._raw_workers.get(tid) is threading.current_thread():
                    self._raw_workers.pop(tid, None)

    def _ensure_raw_binding_by_tid(self, tid: str) -> bool:
        """按 targetId 确保 raw CDP binding 通道在跑。返回是否已就绪。"""
        if not tid:
            return False
        with self._raw_lock:
            t = self._raw_workers.get(tid)
            if t is not None and t.is_alive():
                return True
        t = threading.Thread(target=self._raw_binding_worker, args=(tid,),
                             name=f"dy-raw-{tid[:8]}", daemon=True)
        with self._raw_lock:
            self._raw_workers[tid] = t
        t.start()
        return True

    def _ensure_raw_binding(self, page) -> bool:
        """确保某个 target 有 raw CDP binding 通道在跑。返回是否已就绪。

        关键: Runtime.addBinding 只在「同一条 websocket 连接」存活期间有效,
        所以必须由 _raw_binding_worker 的长连接来做, 不能用 _raw_cdp 那种
        发完就关的短连接(短连接一关 binding 就失效)。
        """
        tid = self._page_debugger_id(page)
        if not tid:
            return False
        return self._ensure_raw_binding_by_tid(tid)

    def _cdp_eval(self, tid, expression):
        """用独立 CDP 对某个 target 做一次 evaluate(半死页也能用)。"""
        r = self._raw_cdp(tid, "Runtime.evaluate",
                          {"expression": expression, "returnByValue": True},
                          timeout=6.0)
        if not isinstance(r, dict):
            return None
        result = r.get("result")
        if not isinstance(result, dict):
            return None
        return result.get("value")

    def _install_hook(self, page):
        """给一个页面装钩子。返回 True 表示确定装好了(或本来就好)。

        v1.7.1: 对空 URL / 非站点页直接跳过 —— 这类页面(典型是切站点时渲染进程
        挂掉留下的空壳)调 page.evaluate 会永久阻塞, 把主循环钉死。已经拉黑的
        页面也不再碰。

        v1.7.2: 「半死页」(page.url 空、page.evaluate 永久挂起, 但 CDP 层还活着,
        典型是快手 new-reco 页)改用独立 CDP 注入钩子 + raw binding 通道回传,
        这样快手收藏事件不再因为 Playwright attach 断裂而丢失。
        """
        url = self._page_url(page)
        if not self._is_site_url(url):
            return False
        if self._page_blacklisted(url):
            return False
        # 先探活再 evaluate: 渲染进程死了的页面, page.evaluate 会永久阻塞。
        # 用 CDP Runtime.evaluate 硬超时探一次, 探不通就拉黑, 绝不碰 page.evaluate。
        if not self._page_alive(page):
            self._blacklist(url, "探活无响应")
            return False

        # v1.7.2: 判断是不是「半死页」—— CDP 能探活但 Playwright 的 url 是空。
        # 这种页绝不能走 page.evaluate(会挂起), 改走独立 CDP 注入。
        try:
            pw_url = page.url or ""
        except Exception:
            pw_url = ""
        tid = self._page_debugger_id(page)
        if not pw_url and tid:
            # 半死页: 先建 raw binding 通道(让 window.__dyReportRaw 就绪),
            # 再用 CDP 注入钩子, 这样 send() 一注入就能走 raw 通道回传。
            self._ensure_raw_binding(page)
            ver = self._cdp_eval(tid, "window.__DY_HOOK_VER__ || 0")
            if ver != HOOK_VER:
                self._raw_cdp(tid, "Runtime.evaluate",
                              {"expression": HOOK_JS}, timeout=8.0)
            self._hook_ok = True
            self._unblacklist(url)
            self.log(f"  · 半死页用 raw 通道注入钩子: {url[:50]}")
            return True

        # 正常页: 走 Playwright evaluate(快、可靠)
        for _ in range(2):
            try:
                if page.evaluate("() => window.__DY_HOOK_VER__ || 0") != HOOK_VER:
                    page.evaluate(HOOK_JS)
                self._hook_ok = True
                self._unblacklist(url)
                return True
            except Exception as e:
                # "Connection closed while reading from the driver" = 这个页面的
                # 渲染进程已经废了, 对它的任何 evaluate 都会卡死, 拉黑别再来。
                if "Connection closed" in str(e) or "Target closed" in str(e):
                    self._blacklist(url, str(e)[:60])
                    return False
                time.sleep(0.6)
        return False

    def _blacklist(self, url, why=""):
        """把一个坏页面拉黑一段时间, 期间不再对它做任何 evaluate。"""
        if not url:
            return
        with self._hook_lock:
            first = url not in self._bad_pages
            self._bad_pages[url] = time.time() + self._BLACKLIST_SECS
        if first:
            self.log(f"  ! 页面没响应, 暂时不碰它: {url[:60]}  {why}")

    def _unblacklist(self, url):
        with self._hook_lock:
            self._bad_pages.pop(url, None)

    def _page_blacklisted(self, url):
        with self._hook_lock:
            until = self._bad_pages.get(url)
            if until is None:
                return False
            if until > time.time():
                return True
            self._bad_pages.pop(url, None)
            return False

    def _hook_all_pages(self, ctx):
        """巡检: 给所有健康的站点页装钩子。坏页跳过, 一个坏页不能拖累整轮。

        v1.7.2: Playwright 对「从打开起就是半死页」的页面完全看不见 ——
        它不进 ctx.pages、也不触发 ctx.on("page")。这种页(典型是快手 new-reco
        页, attach 从 connect_over_cdp 之后才发生且失败)会因此永远装不上钩子、
        收藏事件全丢。所以这里除了遍历 ctx.pages, 还要直接问 CDP /json/list
        拿全部 page target, 对 Playwright 漏掉的站点页走纯 CDP 注入 + raw 通道。
        """
        n = 0
        # 1) 正常可见页走 Playwright evaluate(快、可靠)
        for pg in list(ctx.pages):
            try:
                if pg.is_closed():
                    continue
            except Exception:
                continue
            try:
                if self._install_hook(pg):
                    n += 1
            except Exception:
                pass
        # 2) 补漏: CDP 层里 Playwright 看不见的站点页
        try:
            tabs = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/list",
                timeout=self._PROBE_TIMEOUT).read())
            pages_by_tid = {}
            for pg in list(ctx.pages):
                try:
                    tid = self._page_debugger_id(pg)
                except Exception:
                    tid = None
                if tid:
                    pages_by_tid[tid] = pg
            for t in tabs:
                if not isinstance(t, dict) or t.get("type") != "page":
                    continue
                tid = t.get("id")
                url = t.get("url") or ""
                if not tid or not self._is_site_url(url):
                    continue
                if tid in pages_by_tid:
                    continue  # 已经由 _install_hook 处理过
                # Playwright 看不见的站点页: 直接按 tid 走纯 CDP 通道
                if self._page_blacklisted(url):
                    continue
                if not self._cdp_probe(tid):
                    self._blacklist(url, "探活无响应")
                    continue
                try:
                    self._ensure_raw_binding_by_tid(tid)
                    ver = self._cdp_eval(tid, "window.__DY_HOOK_VER__ || 0")
                    if ver != HOOK_VER:
                        self._raw_cdp(tid, "Runtime.evaluate",
                                      {"expression": HOOK_JS}, timeout=8.0)
                    self._hook_ok = True
                    self._unblacklist(url)
                    self.log(f"  · CDP 补漏注入钩子: {url[:50]}")
                    n += 1
                except Exception:
                    pass
        except Exception:
            pass
        return n

    def _scroll_all(self, page):
        """把收藏列表滚到底, 触发分页加载。"""
        self.log("  正在加载全部收藏…")
        last = 0
        for i in range(120):
            try:
                h = page.evaluate("() => document.body.scrollHeight")
                page.mouse.wheel(0, 2400)
                # v1.6: 旧版整段 120 轮从不 drain(), 钩子塞进 self.queue 的
                # listcollection 响应全堆着(无上界, 长列表吃内存), 而且
                # baseline/回补下载要等滚动全结束、主循环起来才处理 ——
                # 一旦中途异常 break, 这批收藏就整批丢了。每轮顺手排空一次,
                # 边滚边建基线/回补, 响应也不再堆积。
                self.pump(self._ctx)
                self.drain()
                if self._pending_actions:
                    self._resolve_pending()
                if h == last and i > 3:
                    break
                last = h
            except Exception:
                break
        self.log(f"  已加载 {len(self.cache)} 个视频信息")
