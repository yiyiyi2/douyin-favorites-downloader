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
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
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

_SAFE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(name: str, limit: int = 120) -> str:
    """把标题清洗成合法文件名。"""
    name = _SAFE.sub("_", (name or "").strip())
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

    def best(self) -> Candidate | None:
        if not self.candidates:
            return None
        return max(self.candidates, key=lambda c: c.score())


# --------------------------------------------------------------------------
# 从各种形状的抖音 JSON 里抽出视频信息
# --------------------------------------------------------------------------

def _pick_url(url_list: Any) -> str:
    """抖音的 url_list 是候选地址数组, 取第一个可用的 https 地址。"""
    if isinstance(url_list, str):
        return url_list
    if isinstance(url_list, list):
        for u in url_list:
            if isinstance(u, str) and u.startswith("http"):
                return u
    return ""


def _iter_bit_rates(aweme: dict) -> Iterable[dict]:
    """遍历 video 里所有码率描述, 兼容新旧两种结构。"""
    video = aweme.get("video") or {}
    # 新结构: bit_rate[] 每个元素带 gear_name / play_addr
    for br in video.get("bit_rate") or []:
        if isinstance(br, dict):
            yield br
    # 旧结构 / 备用: play_addr_h264 等直挂的 addr
    for key in (
        "play_addr",
        "play_addr_h264",
        "play_addr_265",
        "play_addr_bytevc1",
        "download_addr",
        "play_addr_lowbr",
    ):
        addr = video.get(key)
        if isinstance(addr, dict) and addr.get("url_list"):
            yield {"gear_name": key, "play_addr": addr, "bit_rate": 0}


def _is_watermarked(addr: dict, url: str) -> bool:
    """判断一个地址是不是带水印版本。"""
    if "playwm" in url or "/playwm/" in url:
        return True
    if "watermark=1" in url:
        return True
    # download_addr 在抖音上是『带水印的下载地址』, 默认排除
    if addr.get("is_watermark") is True:
        return True
    return False


def parse_aweme(aweme: dict) -> VideoInfo:
    """把一个 aweme 对象解析成 VideoInfo。"""
    aweme_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or "")
    info = VideoInfo(aweme_id=aweme_id, raw=aweme)

    info.desc = (aweme.get("desc") or "").strip()
    info.create_time = int(aweme.get("create_time") or 0)
    video = aweme.get("video") or {}
    info.duration = int(video.get("duration") or 0)

    author = aweme.get("author") or {}
    info.author = (author.get("nickname") or "").strip()
    info.author_id = str(author.get("uid") or author.get("sec_uid") or "")

    music = aweme.get("music") or {}
    info.music_title = (music.get("title") or "").strip()
    info.music_author = (music.get("author") or "").strip()

    cover = video.get("cover") or video.get("origin_cover") or {}
    info.cover_urls = list(cover.get("url_list") or [])

    # 图集(图文)作品
    images = aweme.get("images")
    if images:
        info.is_image_post = True
        for img in images:
            u = _pick_url((img or {}).get("url_list"))
            if u:
                info.image_urls.append(u)

    # 收集所有视频码率候选
    seen: set[str] = set()
    for br in _iter_bit_rates(aweme):
        addr = br.get("play_addr") or br.get("download_addr") or {}
        if not isinstance(addr, dict):
            continue
        url = _pick_url(addr.get("url_list"))
        if not url or url in seen:
            continue
        if _is_watermarked(addr, url):
            continue
        seen.add(url)
        info.candidates.append(
            Candidate(
                url=url,
                width=int(addr.get("width") or video.get("width") or 0),
                height=int(addr.get("height") or video.get("height") or 0),
                bitrate=int(br.get("bit_rate") or addr.get("data_size") or 0),
                size=int(addr.get("data_size") or 0),
                gear_name=str(br.get("gear_name") or ""),
                quality_key=str(
                    br.get("quality_type") or br.get("quality_key") or ""
                ),
            )
        )

    return info


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
            "Range": "bytes=0-",
        }
    )
    if cookie_header:
        s.headers["Cookie"] = cookie_header
    return s


def probe_size(session: requests.Session, url: str) -> int:
    """用 Range 请求探一下文件总大小, 不下载内容。"""
    try:
        r = session.get(url, stream=True, timeout=15)
        if r.status_code >= 400:
            r.close()
            return 0
        total = int(r.headers.get("Content-Length") or 0)
        cr = r.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)$", cr)
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
            headers = {}
            if pos:
                headers["Range"] = f"bytes={pos}-"
            with session.get(url, stream=True, timeout=30, headers=headers) as r:
                if r.status_code == 416:  # 已经下完了
                    break
                r.raise_for_status()
                mode = "ab" if pos and r.status_code == 206 else "wb"
                if mode == "wb":
                    pos = 0
                total = int(r.headers.get("Content-Length") or 0)
                done = pos
                last = time.time()
                with open(part, mode) as f:
                    for block in r.iter_content(chunk_size=chunk):
                        if not block:
                            continue
                        f.write(block)
                        done += len(block)
                        if time.time() - last > 0.5:
                            if on_progress:
                                try:
                                    on_progress(done, pos + total)
                                except Exception:
                                    pass
                        if time.time() - last > 1.0:
                            pct = f"{done * 100 / (pos + total):5.1f}%" if total else "  ?  "
                            log(f"      {pct}  {done / 1048576:7.1f} MB")
                            last = time.time()
            if part.exists() and part.stat().st_size > 0:
                if on_progress:
                    try:
                        final = part.stat().st_size
                        on_progress(final, final)
                    except Exception:
                        pass
                part.replace(dest)
                return True
        except Exception as e:  # noqa: BLE001
            log(f"      ! 第 {attempt} 次尝试失败: {e}")
            time.sleep(1.5 * attempt)

    return False


def verify_video(path: Path) -> bool:
    """
    轻量校验: 确认真的是个视频文件。
    优先 ffprobe, 没有就退回魔数检查。
    """
    if not path.exists() or path.stat().st_size < 4096:
        return False
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0 and out.stdout.strip():
            float(out.stdout.strip())
            return True
        if out.returncode != 0:
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        return False

    head = path.open("rb").read(16)
    # MP4 / MOV: 第 4-8 字节是 'ftyp'
    return len(head) >= 12 and head[4:8] == b"ftyp"


def build_filename(info: VideoInfo, index: int = 0) -> str:
    """生成 时间_作者_标题_awemeid 形式的文件名。"""
    parts = []
    if info.create_time:
        parts.append(time.strftime("%Y%m%d", time.localtime(info.create_time)))
    if info.author:
        parts.append(safe_name(info.author, 30))
    title = safe_name(info.desc, 70) if info.desc else "无标题"
    parts.append(title)
    stem = "_".join(p for p in parts if p)
    # 加 aweme_id 后缀保证唯一, 避免同名覆盖
    return f"{stem}_{info.aweme_id}"
