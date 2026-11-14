#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音收藏自动下载 —— 核心下载模块

职责:
  1. 解析视频 ID / 短链
  2. 通过「浏览器内实测」拿到该视频所有可用的码率(清晰度)列表
  3. 选最高画质 + 去水印地址, 下载落盘

关键设计: 抖音的 play_addr 里带水印的地址和不带水印的地址只有
`ratio` / `watermark` 参数的差别; 前端页面拿到的 play_addr 就已经是
无水印版本(playwm 才是水印版)。所以我们优先使用页面里拿到的地址,
并剔除掉任何 playwm / watermark=1 的候选。

v1.6 健壮性: 抖音接口的字段类型/结构随时可能变, 这里统一加了一层类型
归一化(_as_dict/_as_list/_as_str/_as_int), 任何字段变成字符串、数字、
列表、null 都不会让整条下载链抛异常。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

# 抖音网页端接口取视频详情用的 Referer, 缺了会被 403
REFERER = "https://www.douyin.com/"

# v1.7 快手。实测快手 web 的 djvod/kwaicdn 直链是完全公开的:
# 带不带 Cookie/Referer 都返回 200 video/mp4, 而且几十分钟前抓到的地址
# 仍然能下(不像抖音签名几分钟就废)。所以快手这边不需要登录态,
# 也不需要每次重新抓详情 —— 直接用页面自己加载 feed 时给的那份地址。
REFERER_KS = "https://www.kuaishou.com/"

_SAFE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_NUM_RE = re.compile(r"-?\d+")

# 主程序用 pythonw.exe 跑(没有控制台)。这种进程里起任何子进程, Windows 都会
# 给它新开一个控制台窗口 —— 用户看到的就是"下完突然弹个黑框又没了"。
# ffmpeg 合并音视频、ffprobe 校验、yt-dlp 解析、powershell 探测进程都会闪。
# 统一加上 CREATE_NO_WINDOW 就能彻底不闪。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_hidden(cmd, **kw):
    """跑一个不该弹窗的子进程。所有子进程调用都走这里。"""
    kw.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, **kw)


def popen_hidden(cmd, **kw):
    """起一个不该弹窗的后台进程。"""
    kw.setdefault("creationflags", _NO_WINDOW)
    return subprocess.Popen(cmd, **kw)


# --------------------------------------------------------------------------
# 类型归一化 —— 抖音接口字段的类型说变就变, 这里统一挡一层
# --------------------------------------------------------------------------

def _as_dict(v: Any) -> dict:
    """不是 dict 就当空 dict, 避免 .get 抛 AttributeError。"""
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list:
    """把任意值包成可迭代 list; None 当空, 标量当单元素。"""
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    if v is None:
        return []
    return [v]


def _as_str(v: Any) -> str:
    """把任意值变成可用的字符串; 容器/布尔一律当空串。"""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return ""
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return ""
        return str(int(v)) if v.is_integer() else str(v)
    return ""


def _as_int(v: Any, default: int = 0) -> int:
    """把任意值变成整数; '1080p' / '15s' / '1.5M' 这类取开头数字。"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return int(v)
    if isinstance(v, str):
        m = _NUM_RE.search(v)
        if m:
            try:
                return int(m.group())
            except (ValueError, OverflowError):
                return default
    return default


def safe_name(name: Any, limit: int = 120) -> str:
    """把标题清洗成合法文件名。"""
    name = _SAFE.sub("_", _as_str(name).strip())
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if len(name) > limit:
        name = name[:limit].rstrip(" ._")
    return name or "untitled"


@dataclass
class Candidate:
    """一个可下载的视频流候选。"""
    url: str
    width: int = 0
    height: int = 0
    bitrate: int = 0
    size: int = 0
    gear_name: str = ""
    quality_key: str = ""
    # --- 以下只有 B 站用得上（DASH 音视频分离，要下两路再合并）---
    audio_url: str = ""          # 独立音频轨地址; 空 = 单流(抖音/快手/老B站)
    audio_bitrate: int = 0
    alt_urls: list[str] = field(default_factory=list)   # backup 地址, 逐个重试

    @property
    def area(self) -> int:
        return max(self.width, 0) * max(self.height, 0)

    def score(self) -> tuple:
        """
        排序键 —— 越大越好。
        主序: 分辨率面积 (最高画质的第一优先)
        次序: 码率 (同分辨率下取更高码率)
        再次: 文件体积
        """
        return (self.area, self.bitrate, self.size)

    @property
    def needs_merge(self) -> bool:
        """是否需要把独立音频轨合并进来（B 站 DASH）。"""
        return bool(self.audio_url)


@dataclass
class VideoInfo:
    aweme_id: str
    desc: str = ""
    author: str = ""
    author_id: str = ""
    create_time: int = 0
    duration: int = 0          # 毫秒
    cover_urls: list[str] = field(default_factory=list)
    music_title: str = ""
    music_author: str = ""
    is_image_post: bool = False
    image_urls: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    platform: str = "douyin"   # douyin / kuaishou / bilibili

    def best(self) -> Candidate | None:
        if not self.candidates:
            return None
        return max(self.candidates, key=lambda c: c.score())


# --------------------------------------------------------------------------
# 从各种形状的抖音 JSON 里抽出视频信息
# --------------------------------------------------------------------------

def _as_url_list(v: Any) -> list[str]:
    """把 url_list / url / 嵌套 dict / 单字符串统一成字符串列表。"""
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, dict):
        for k in ("url_list", "url", "src", "download_url", "uri"):
            if k in v:
                return _as_url_list(v[k])
        return []
    if isinstance(v, (list, tuple)):
        out: list[str] = []
        for u in v:
            out.extend(_as_url_list(u))
        return out
    return []


def _pick_url(url_list: Any) -> str:
    """抖音的 url_list 是候选地址数组, 取第一个可用的 https 地址。"""
    urls = _as_url_list(url_list)
    for u in urls:
        if isinstance(u, str) and u.startswith("http"):
            return u
    for u in urls:
        if isinstance(u, str) and u:
            return u
    return ""


def _iter_bit_rates(aweme: Any) -> Iterable[dict]:
    """遍历 video 里所有码率描述, 兼容新旧两种结构。"""
    aweme = _as_dict(aweme)
    video = _as_dict(aweme.get("video"))
    # 新结构: bit_rate[] 每个元素带 gear_name / play_addr
    for br in _as_list(video.get("bit_rate")):
        if isinstance(br, dict):
            yield br
    # 旧结构 / 备用: play_addr_h264 等直挂的 addr。
    # 不再写死 bit_rate=0 —— addr 里若自带码率就用它, 让同分辨率的多条直挂流也能排序。
    for key in (
        "play_addr",
        "play_addr_h264",
        "play_addr_265",
        "play_addr_bytevc1",
        "download_addr",
        "play_addr_lowbr",
    ):
        addr = video.get(key)
        if isinstance(addr, str) and addr.startswith("http"):
            yield {"gear_name": key, "play_addr": addr, "bit_rate": 0}
        elif isinstance(addr, dict) and (addr.get("url_list") or addr.get("url")):
            yield {
                "gear_name": key,
                "play_addr": addr,
                "bit_rate": _as_int(addr.get("bit_rate"), 0),
            }


def _is_watermarked(addr: Any, url: str) -> bool:
    """判断一个地址是不是带水印版本。"""
    addr = _as_dict(addr)
    if not isinstance(url, str):
        return True
    low = url.lower()
    if "playwm" in low or "/playwm/" in low:
        return True
    if "watermark=1" in low or "watermark=true" in low:
        return True
    wm = addr.get("is_watermark")
    if wm is True or wm == 1 or (
        isinstance(wm, str) and wm.lower() in ("1", "true", "yes")
    ):
        return True
    return False


def parse_aweme(aweme: Any) -> VideoInfo:
    """把一个 aweme 对象解析成 VideoInfo。任何字段类型异常都不会抛错。"""
    aweme = _as_dict(aweme)
    aweme_id = _as_str(
        aweme.get("aweme_id") or aweme.get("awemeId")
        or aweme.get("itemId") or aweme.get("object_id") or ""
    )
    info = VideoInfo(aweme_id=aweme_id, raw=aweme)

    info.desc = _as_str(aweme.get("desc")).strip()
    # 抖音偶尔会把 create_time 给成毫秒(13 位), 统一折回秒; 负数/字符串归一化后再夹取。
    ct = _as_int(aweme.get("create_time"), 0)
    if ct > 100_000_000_000:
        ct //= 1000
    info.create_time = ct if ct > 0 else 0

    video = _as_dict(aweme.get("video"))
    info.duration = _as_int(video.get("duration"), 0)

    author = _as_dict(aweme.get("author"))
    info.author = _as_str(author.get("nickname")).strip()
    info.author_id = _as_str(author.get("uid") or author.get("sec_uid") or "")

    music = _as_dict(aweme.get("music"))
    info.music_title = _as_str(music.get("title")).strip()
    info.music_author = _as_str(music.get("author")).strip()

    cover = _as_dict(video.get("cover") or video.get("origin_cover"))
    info.cover_urls = [u for u in _as_url_list(cover.get("url_list")) if u]

    # 图集(图文)作品。images 变成 dict/字符串时不崩, 也尽量还能抠出图片地址。
    images = aweme.get("images")
    if images:
        info.is_image_post = True
        for img in _as_list(images):
            if isinstance(img, str):
                u = img if img.startswith("http") else ""
            else:
                u = _pick_url(_as_dict(img).get("url_list") or img)
            if u:
                info.image_urls.append(u)

    # 收集所有视频码率候选
    seen: set[str] = set()
    for br in _iter_bit_rates(aweme):
        br = _as_dict(br)
        addr_raw = br.get("play_addr") or br.get("download_addr") or {}
        # play_addr 直接变成字符串(而不是 dict)时也要能收下这个候选。
        if isinstance(addr_raw, str):
            addr: dict = {}
            url = addr_raw if addr_raw.startswith("http") else ""
        else:
            addr = _as_dict(addr_raw)
            url = _pick_url(addr.get("url_list") or addr.get("url"))
        if not url or url in seen:
            continue
        if _is_watermarked(addr, url):
            continue
        seen.add(url)
        info.candidates.append(
            Candidate(
                url=url,
                width=_as_int(addr.get("width") or video.get("width"), 0),
                height=_as_int(addr.get("height") or video.get("height"), 0),
                bitrate=_as_int(br.get("bit_rate") or addr.get("data_size"), 0),
                size=_as_int(addr.get("data_size"), 0),
                gear_name=_as_str(br.get("gear_name")),
                quality_key=_as_str(
                    br.get("quality_type") or br.get("quality_key") or ""
                ),
            )
        )

    return info


# --------------------------------------------------------------------------
# v1.7 快手
#
# 快手 web 的码率信息和抖音完全不是一个形状: 没有 bit_rate[],
# 而是 manifest.adaptationSet[].representation[], 每一档带
# url / backupUrl[] / width / height / avgBitrate / fileSize / qualityType。
# 详情页更是把数据塞进 Apollo 客户端缓存(window.__APOLLO_STATE__),
# 里面用 {"type":"id","id":"VisionVideoSetRepresentation:1"} 这种引用形式
# 间接指向真正的对象, 必须解引用才能拿到地址 —— 这里统一处理两种形状。
# --------------------------------------------------------------------------

PLATFORM_DOUYIN = "douyin"
PLATFORM_KUAISHOU = "kuaishou"
PLATFORM_BILIBILI = "bilibili"

_KS_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")


def is_kuaishou_id(v: Any) -> bool:
    """快手的 photoId 形如 `3xb42hegyea3mzc`(字母数字混排), 抖音是纯数字。

    判定规则刻意宽松: 8~32 位、只含字母数字/下划线/连字符、且字母和数字都有。
    多认几个不会有坏处(调用方拿到后用真实接口再确认), 少认一个就永远下不了。
    """
    s = _as_str(v)
    if not (8 <= len(s) <= 32) or not _KS_ID_RE.match(s):
        return False
    return any(c.isdigit() for c in s) and any(c.isalpha() for c in s)


def _ks_deref(v: Any, client: dict | None = None, depth: int = 0) -> Any:
    """解开 Apollo 缓存里的引用。

    `{"type":"id","id":"VisionVideoAdaptationSet:1"}` -> client[那个 key]
    `{"type":"json","json":{...}}`                    -> 里面的 json
    其它形状原样(递归)返回。client 为 None 时只解 json 壳。
    """
    if depth > 8:
        return v
    if isinstance(v, list):
        return [_ks_deref(x, client, depth + 1) for x in v]
    if not isinstance(v, dict):
        return v
    t = v.get("type")
    if t == "json" and "json" in v:
        return _ks_deref(v.get("json"), client, depth + 1)
    if t == "id" and client is not None:
        key = v.get("id")
        if isinstance(key, str) and key in client:
            return _ks_deref(client.get(key), client, depth + 1)
        return {}
    return {k: _ks_deref(x, client, depth + 1) for k, x in v.items()}


def _ks_iter_representations(photo: Any, client: dict | None = None) -> Iterable[dict]:
    """遍历快手 photo 里的所有码率档(含 H.265 那份 manifest)。"""
    photo = _as_dict(photo)
    for key in ("manifest", "manifestH265"):
        mf = _ks_deref(photo.get(key), client)
        if not isinstance(mf, dict):
            continue
        for aset in _as_list(_ks_deref(mf.get("adaptationSet"), client)):
            for rep in _as_list(_ks_deref(_as_dict(aset).get("representation"), client)):
                rep = _as_dict(rep)
                if rep.get("url") or rep.get("backupUrl"):
                    yield rep


def _ks_rep_urls(rep: dict, client: dict | None) -> list[str]:
    """一档码率可能给主地址 + 若干 backupUrl, 全部收下当候选(下不动就换)。"""
    urls: list[str] = []
    u = _as_str(rep.get("url"))
    if u.startswith("http"):
        urls.append(u)
    bu = _ks_deref(rep.get("backupUrl"), client)
    for x in _as_list(bu if isinstance(bu, list) else [bu]):
        x = _as_str(x)
        if x.startswith("http") and x not in urls:
            urls.append(x)
    return urls


def parse_kuaishou(photo: Any, client: dict | None = None) -> VideoInfo:
    """把一个快手 photo 对象解析成 VideoInfo。

    接受三种入参, 都能吃:
      1. REST 接口里的 feeds[i].photo 原样
      2. Apollo 里的 VisionVideoDetailPhoto:<id>(需同时给 client 才能解引用)
      3. watcher 包的一层 {"aweme_id":..., "_platform":"kuaishou", "_ks": {...}}
    """
    photo = _as_dict(photo)
    inner = _as_dict(photo.get("_ks"))
    if inner:
        if client is None:
            client = _as_dict(photo.get("_ks_client")) or None
        photo = inner
    if client is None:
        client = _as_dict(photo.get("_ks_client")) or None

    pid = _as_str(photo.get("id") or photo.get("photoId") or photo.get("photo_id"))
    info = VideoInfo(aweme_id=pid, raw=photo)

    info.desc = _as_str(photo.get("caption")).strip()
    ts = _as_int(photo.get("timestamp"), 0)
    if ts > 100_000_000_000:          # 快手的 timestamp 是毫秒
        ts //= 1000
    info.create_time = ts if ts > 0 else 0
    # 快手 duration 本来就是毫秒, 和抖音一致, 不用换算
    info.duration = _as_int(photo.get("duration"), 0)

    author = _as_dict(_ks_deref(photo.get("author"), client))
    info.author = _as_str(author.get("name") or author.get("userName")).strip()
    info.author_id = _as_str(author.get("id") or author.get("userId") or "")

    cover = _as_str(photo.get("coverUrl") or photo.get("animatedCoverUrl"))
    if cover.startswith("http"):
        info.cover_urls = [cover]

    seen: set[str] = set()
    for rep in _ks_iter_representations(photo, client):
        w = _as_int(rep.get("width"), 0)
        h = _as_int(rep.get("height"), 0)
        # avgBitrate 是"平均码率"(单位 kbps), maxBitrate 是峰值。
        # 排序想用平均码率(更接近真实清晰度), 拿不到再退峰值。
        br = _as_int(rep.get("avgBitrate") or rep.get("maxBitrate"), 0)
        size = _as_int(rep.get("fileSize"), 0)
        qk = _as_str(rep.get("qualityType") or "")
        gear = _as_str(rep.get("qualityLabel") or "")
        for url in _ks_rep_urls(rep, client):
            if url in seen or _is_watermarked({}, url):
                continue
            seen.add(url)
            info.candidates.append(
                Candidate(url=url, width=w, height=h, bitrate=br, size=size,
                          gear_name=gear, quality_key=qk)
            )

    # 兜底: manifest 结构一变(或页面只给了 photoUrl)时, 至少还能下 720p
    if not info.candidates:
        u = _as_str(photo.get("photoUrl"))
        if u.startswith("http"):
            info.candidates.append(
                Candidate(url=u,
                          width=_as_int(photo.get("width"), 0),
                          height=_as_int(photo.get("height"), 0),
                          gear_name="photoUrl")
            )

    return info


def parse_any(obj: Any) -> VideoInfo:
    """按平台自动选择解析器。快手带 `_platform`/`_ks`, B站带 `_platform`/`_view`。"""
    obj = _as_dict(obj)
    if obj.get("_platform") == PLATFORM_KUAISHOU or obj.get("_ks"):
        return parse_kuaishou(obj)
    if obj.get("_platform") == PLATFORM_BILIBILI or obj.get("_view"):
        # 延迟导入: bili 依赖 core, 顶层导入会成环
        from . import bili
        return bili.parse_bili_wrapped(obj)
    return parse_aweme(obj)


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------

def build_session(cookie_header: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Referer": REFERER,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    # v1.6: 不在 session 上放全局 Range。以前放 "bytes=0-", 导致连全新下载
    # 也会被当成断点请求, 有些 CDN 回 206 + 只剩一部分的 Content-Length,
    # 于是 expect 算错、明明下完却判成不完整。Range 只在 download() 里按需加。
    if cookie_header:
        s.headers["Cookie"] = cookie_header
    return s


def probe_size(session: requests.Session, url: str) -> int:
    """用 Range 请求探一下文件总大小, 不下载内容。"""
    try:
        r = session.get(url, stream=True, timeout=15,
                        headers={"Range": "bytes=0-1"})
        if r.status_code >= 400:
            r.close()
            return 0
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype or "application/json" in ctype:
            r.close()
            return 0
        total = _as_int(r.headers.get("Content-Length"), 0)
        cr = r.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            total = int(m.group(1))
        r.close()
        return total
    except Exception:
        return 0


def download(
    session: requests.Session,
    url: str,
    dest: Path,
    chunk: int = 1 << 20,
    retries: int = 3,
    log=print,
    on_progress=None,
) -> bool:
    """带重试与断点续传的下载。

    on_progress(done, total) 是给界面用的进度回调, 拿到的是「已下载字节 / 总字节」,
    total 未知时是 0。回调里的异常一律吞掉 —— 界面挂了不该影响下载。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    for attempt in range(1, retries + 1):
        try:
            pos = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={pos}-"} if pos else {}
            with session.get(url, stream=True, timeout=30, headers=headers) as r:
                if r.status_code == 416:
                    # 416 = 请求的起点已经超出资源长度, 说明 .part 其实已经下完整了。
                    if part.exists() and part.stat().st_size > 0:
                        part.replace(dest)
                        return True
                    # .part 是空的(或不存在)却还收到 416: 服务端/断点状态不对。
                    # 以前直接 break, 会留下一个空 .part, 下次重试还是 416,
                    # 这条视频就永远下不下来。现在删掉断点重新完整 GET。
                    try:
                        part.unlink()
                    except OSError:
                        pass
                    log("      ! 416 且断点为空, 删掉断点重新完整下载")
                    continue
                r.raise_for_status()
                ctype = (r.headers.get("Content-Type") or "").lower()
                if ("text/html" in ctype or "application/json" in ctype
                        or "text/plain" in ctype):
                    # 302 到登录页 / 接口返错: 别把 HTML 当视频落盘。
                    log("      ! 返回的不是视频(网页/JSON), 换候选")
                    return False
                mode = "ab" if pos and r.status_code == 206 else "wb"
                if mode == "wb":
                    pos = 0
                total = _as_int(r.headers.get("Content-Length"), 0)
                cr = r.headers.get("Content-Range") or ""
                m = re.search(r"/(\d+)\s*$", cr)
                # 有些 CDN 在 206 时把 Content-Length 给成「全量体积」而不是「剩余字节」,
                # 只看 pos+total 会让 got<expect 恒真、明明下完还重试 3 次。
                # Content-Range 里的 /总长 才是权威值。
                expect = int(m.group(1)) if m else (pos + total if total else 0)
                done = pos
                last = time.time()
                bad_content = False
                sniff = mode == "wb"
                with open(part, mode) as f:
                    for block in r.iter_content(chunk_size=chunk):
                        if not block:
                            continue
                        if sniff:
                            sniff = False
                            probe = bytes(block[:64]).lstrip()
                            if probe.startswith((b"<!", b"<html", b"<?xml",
                                                 b"{", b"[")):
                                bad_content = True
                                break
                        f.write(block)
                        done += len(block)
                        if time.time() - last > 0.5:
                            if on_progress:
                                try:
                                    on_progress(done, expect)
                                except Exception:
                                    pass
                        if time.time() - last > 1.0:
                            pct = (f"{done * 100 / expect:5.1f}%"
                                   if expect else "  ?  ")
                            log(f"      {pct}  {done / 1048576:7.1f} MB")
                            last = time.time()
                if bad_content:
                    log("      ! 内容看起来是网页而不是视频, 换候选")
                    try:
                        part.unlink()
                    except OSError:
                        pass
                    return False
            got = part.stat().st_size if part.exists() else 0
            if expect and got < expect:
                # 连接被中途掐断但没抛异常: 这时 replace 会留下一个后半段是空洞的视频,
                # verify_video 只看文件头是查不出来的。留着 .part, 下一轮用 Range 续传。
                log(f"      ! 只下到 {got}/{expect} 字节, 继续重试")
                time.sleep(1.0)
                continue
            if got > 0:
                if on_progress:
                    try:
                        on_progress(got, expect or got)
                    except Exception:
                        pass
                part.replace(dest)
                return True
        except Exception as e:  # noqa: BLE001
            log(f"      ! 第 {attempt} 次尝试失败: {e}")
            time.sleep(1.5 * attempt)

    return False


# ---------------------------------------------------------------- 分段并发下载
# v2.2: 大文件单连接会被 CDN 按连接限速。实测 B站 448MB 的 4K 轨:
#   单连接顺序 1.96 MB/s, 8 线程并发 13.37 MB/s (6.83 倍)。
# 所以超过阈值的文件切成段并发拉, 每段自己带 Range, 各自重试。
# 小文件不值得(建连开销比下载还大), 仍然走单连接。

PARALLEL_MIN_SIZE = 8 << 20        # 小于 8MB 不并发
PARALLEL_MIN_SEG = 2 << 20         # 每段至少 2MB
PARALLEL_MAX_WORKERS = 8           # 默认 8 个连接(实测 8 路已接近上限)
PARALLEL_MAX_WORKERS_CAP = 16      # 用户可配到 16


def _seg_plan(total: int, workers: int) -> list[tuple[int, int]]:
    """把 total 字节切成 workers 段, 返回 [(start, end_inclusive), ...]。"""
    if total <= 0 or workers <= 0:
        return []
    seg = max(PARALLEL_MIN_SEG, (total + workers - 1) // workers)
    plan = []
    start = 0
    while start < total:
        end = min(start + seg, total) - 1
        plan.append((start, end))
        start = end + 1
    return plan


def download_parallel(
    session: requests.Session,
    url: str,
    dest: Path,
    workers: int = PARALLEL_MAX_WORKERS,
    log=print,
    on_progress=None,
    headers: dict | None = None,
    alt_urls: Iterable[str] = (),
) -> bool:
    """分段并发下载一个大文件。

    先用 Range 探出真实总长, 再按段并发(每段一个连接)写到同一个文件的
    不同偏移。任何一段彻底失败就整体返回 False, 让上层换候选/重试。
    进度回调拿到的是**所有段已完成字节之和**, 所以面板上是真实进度。

    为什么要并发: CDN 通常对单条 TCP 连接限速, 而不是对账号限速。
    实测同一个 B站 4K 轨, 单连接 1.96 MB/s, 8 路并发 13.37 MB/s。
    """
    hdrs = dict(headers or {})
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    # 1) 探总长。同时确认服务端**真的支持 Range** —— 只有 206 +
    #    Content-Range 才算数; 忽略 Range 直接回 200 全量体的服务端
    #    如果也当成支持, 每段都会从头开始写, 拼出来是错位垃圾。
    total = 0
    urls = [url] + [u for u in alt_urls if u]
    ranged_ok = False
    for u in urls:
        try:
            r = session.get(u, headers={**hdrs, "Range": "bytes=0-0"},
                            timeout=30, stream=True)
            try:
                if r.status_code >= 400:
                    continue
                cr = r.headers.get("Content-Range") or ""
                m = re.search(r"/(\d+)\s*$", cr)
                if m and r.status_code == 206:
                    total = int(m.group(1))
                    url = u
                    ranged_ok = True
                    break
            finally:
                r.close()
        except Exception:  # noqa: BLE001
            continue

    if not ranged_ok:
        log("      ! 服务端不支持 Range, 退回单连接")
        return False
    if total <= 0:
        log("      ! 分段下载探不到总长度, 退回单连接")
        return False

    plan = _seg_plan(total, max(1, min(workers, PARALLEL_MAX_WORKERS_CAP)))
    if len(plan) <= 1:
        log("      ! 文件太小或段数不足, 退回单连接")
        return False

    log(f"      分段并发: {total / 1048576:.1f} MB / {len(plan)} 段 "
        f"/ {min(workers, PARALLEL_MAX_WORKERS_CAP)} 连接")

    # 2) 预分配文件, 让各线程按偏移写
    try:
        with open(part, "wb") as f:
            f.truncate(total)
    except OSError as e:
        log(f"      ! 预分配 {part.name} 失败: {e}")
        return False

    done_bytes = 0
    lock = threading.Lock()
    last_emit = [0.0]

    def _bump(n: int, force: bool = False):
        nonlocal done_bytes
        with lock:
            done_bytes += n
            now = time.monotonic()
            if on_progress and (force or now - last_emit[0] >= 0.5):
                last_emit[0] = now
                try:
                    on_progress(done_bytes, total)
                except Exception:  # noqa: BLE001
                    pass

    def _fetch(start: int, end: int, tries: int = 3) -> bool:
        nonlocal total
        for attempt in range(1, tries + 1):
            try:
                r = session.get(
                    url, headers={**hdrs, "Range": f"bytes={start}-{end}"},
                    timeout=60, stream=True)
                try:
                    if r.status_code == 416:
                        return True          # 段尾正好等于总长, 已齐全
                    if r.status_code >= 400:
                        raise IOError(f"HTTP {r.status_code}")
                    if r.status_code != 206:
                        # 服务端把 Range 忽略了, 回的是整个文件。
                        # 继续写会把内容错位塞进这一段的偏移里。
                        raise IOError(f"服务端没按 Range 返回(HTTP {r.status_code})")
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if "text/html" in ctype or "application/json" in ctype:
                        raise IOError("返回的不是视频")
                    want = end - start + 1
                    got = 0
                    with open(part, "r+b") as f:
                        f.seek(start)
                        for block in r.iter_content(1 << 16):
                            if not block:
                                continue
                            f.write(block)
                            got += len(block)
                            _bump(len(block))
                            if got >= want:
                                break
                    if got < want:
                        # 断流: 下次从断点续
                        raise IOError(f"只到 {got}/{want}")
                    return True
                finally:
                    r.close()
            except Exception as e:  # noqa: BLE001
                if attempt >= tries:
                    log(f"      ! 段 {start}-{end} 失败: {str(e)[:60]}")
                    return False
                time.sleep(1.0 * attempt)
                # 重试时缩小范围, 从已经写好的位置继续
                try:
                    have = 0
                    with open(part, "rb") as f:
                        f.seek(start)
                        have = len(f.read(end - start + 1))
                    # 粗略判断: 文件是预分配的, 无法区分空洞,
                    # 所以重试整段(幂等覆盖写)。
                except Exception:  # noqa: BLE001
                    pass
        return False

    try:
        ok_all = True
        with ThreadPoolExecutor(max_workers=min(workers,
                                                PARALLEL_MAX_WORKERS_CAP)) as ex:
            futs = {ex.submit(_fetch, s, e): (s, e) for s, e in plan}
            for fut in as_completed(futs):
                if not fut.result():
                    ok_all = False
        if not ok_all:
            try:
                part.unlink()
            except OSError:
                pass
            return False
        have = part.stat().st_size if part.exists() else 0
        if have != total:
            log(f"      ! 分段下载后大小不对: {have}/{total}")
            try:
                part.unlink()
            except OSError:
                pass
            return False
        if on_progress:
            try:
                on_progress(total, total)
            except Exception:  # noqa: BLE001
                pass
        part.replace(dest)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"      ! 分段下载异常: {str(e)[:80]}")
        try:
            part.unlink()
        except OSError:
            pass
        return False


def download_smart(
    session: requests.Session,
    url: str,
    dest: Path,
    log=print,
    on_progress=None,
    headers: dict | None = None,
    alt_urls: Iterable[str] = (),
    workers: int = PARALLEL_MAX_WORKERS,
) -> bool:
    """大文件走分段并发, 小文件走原来的单连接(带断点续传)。"""
    size = probe_size(session, url)
    if size >= PARALLEL_MIN_SIZE and workers > 1:
        if download_parallel(session, url, dest, workers=workers, log=log,
                             on_progress=on_progress, headers=headers,
                             alt_urls=alt_urls):
            return True
        log("      · 分段并发没成功, 退回单连接")
    return download(session, url, dest, log=log, on_progress=on_progress)


def verify_video(path: Path) -> bool:
    """轻量校验: 确认真的是个视频文件。
    优先 ffprobe; ffprobe 不在、超时、或判定失败时**退回魔数检查**,
    而不是直接判 False —— 以前 `returncode != 0 -> return False` 让魔数
    兜底永远不可达, 结果「下完的文件被当成坏文件删掉」。
    魔数也覆盖 WebM/FLV/AVI/MPEG-TS, 不只认 MP4。
    """
    try:
        if not path.exists() or path.stat().st_size < 512:
            return False
    except OSError:
        return False

    try:
        out = run_hidden(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0 and out.stdout.strip():
            try:
                float(out.stdout.strip())
                return True
            except ValueError:
                pass  # ffprobe 说成功但输出解析不了 -> 交给魔数兜底
        # returncode != 0 / 输出为空: 不再直接判 False, 交给魔数兜底
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        pass  # 任何 ffprobe 异常都退回魔数检查

    try:
        with path.open("rb") as f:
            head = f.read(256)
    except OSError:
        return False
    if len(head) < 12:
        return False
    if head[4:8] == b"ftyp":                    # MP4 / MOV / 3GP
        return True
    if head[:4] == b"\x1a\x45\xdf\xa3":        # WebM / MKV (EBML)
        return True
    if head[:3] == b"FLV":                      # FLV
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":   # AVI
        return True
    if head[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):  # MPEG-PS
        return True
    if len(head) >= 189 and head[0] == 0x47 and head[188] == 0x47:  # MPEG-TS
        return True
    return False


def build_filename(info: VideoInfo, index: int = 0) -> str:
    """生成 时间_作者_标题_awemeid 形式的文件名。

    index 保留只为兼容旧调用(当前无人使用)。create_time 是毫秒/负数/字符串
    时都必须能出文件名, 绝不让 time.localtime 抛 OSError 拖垮整条下载链。
    """
    parts: list[str] = []
    ct = _as_int(info.create_time, 0)
    if ct > 100_000_000_000:
        ct //= 1000
    if ct > 0:
        try:
            parts.append(time.strftime("%Y%m%d", time.localtime(ct)))
        except (OSError, OverflowError, ValueError):
            pass
    author = _as_str(info.author)
    if author:
        parts.append(safe_name(author, 30))
    desc = _as_str(info.desc)
    title = safe_name(desc, 70) if desc else "无标题"
    parts.append(title)
    stem = "_".join(p for p in parts if p)
    aid = _as_str(info.aweme_id) or "unknown"
    # 加 aweme_id 后缀保证唯一, 避免同名覆盖
    return f"{stem}_{aid}"
