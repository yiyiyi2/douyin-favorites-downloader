"""
B 站（bilibili）接入。

和抖音/快手最大的不同：
  1. 数据不用浏览器签名 —— `x/web-interface/view` 和 `x/player/playurl`
     直接 requests 就能拿（实测不需要 wbi 签名）。
  2. 画质走 DASH，**音视频是分开的两条流**，必须下两路再用 ffmpeg 合并。
     抖音/快手的播放地址是音视频合一的 mp4，所以那边不需要这一步。
  3. 画质上限由登录态决定：不登录最多 480P，普通登录 1080P，
     大会员才有 1080P+/4K/8K。这里永远请求 `qn=127, fourk=1`，
     能拿到多少由 B 站自己按 cookie 决定 —— 登录了就自动变高。
  4. 直链下载**不需要 Referer**（实测三种 Referer 都返回 206）。

收藏动作接口：`POST /x/v3/fav/resource/deal`，form 体：
  rid=<aid>&type=2&add_media_ids=<收藏夹id>&del_media_ids=&csrf=<bili_jct>
  收藏 = add_media_ids 非空；取消收藏 = del_media_ids 非空。
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from . import core
from .core import (
    Candidate,
    VideoInfo,
    _as_int,
    _as_str,
    _as_url_list,
    PLATFORM_BILIBILI,
    safe_name,
)

REFERER_BILI = "https://www.bilibili.com/"
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYURL = "https://api.bilibili.com/x/player/playurl"

# BV 号: BV + 10 位 base58-ish；av 号: av + 数字
_BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
_AV_RE = re.compile(r"^av(\d+)$", re.IGNORECASE)

# B 站清晰度 id → 名字（用于日志和面板显示）
QUALITY_NAMES = {
    127: "8K",
    126: "杜比视界",
    125: "HDR",
    120: "4K",
    116: "1080P60",
    112: "1080P+",
    80: "1080P",
    74: "720P60",
    64: "720P",
    32: "480P",
    16: "360P",
    6: "240P",
}


def is_bili_id(v: Any) -> bool:
    """判断是不是 B 站的视频 id（BV 号或 av 号）。"""
    s = _as_str(v).strip()
    if not s:
        return False
    return bool(_BV_RE.match(s) or _AV_RE.match(s))


def is_bili_url(url: str) -> bool:
    u = (url or "").lower()
    return "bilibili.com" in u or "b23.tv" in u


def bv_from_url(url: str) -> str:
    """从 B 站各种 URL 里抠出 BV/av 号。"""
    u = url or ""
    m = re.search(r"(BV[0-9A-Za-z]{10})", u)
    if m:
        return m.group(1)
    m = re.search(r"/av(\d+)", u, re.IGNORECASE)
    if m:
        return "av" + m.group(1)
    return ""


def parse_bili_view(view: Any, bvid: str = "") -> VideoInfo:
    """
    解析 `x/web-interface/view` 的 data 段。

    这里**不填 candidates** —— DASH 轨要另外请求 playurl 才有。
    由 `attach_dash()` 补上。
    """
    v = view if isinstance(view, dict) else {}
    bid = _as_str(v.get("bvid")) or bvid
    if not bid:
        aid = _as_int(v.get("aid"), 0)
        bid = f"av{aid}" if aid else ""

    owner = v.get("owner") if isinstance(v.get("owner"), dict) else {}
    info = VideoInfo(
        aweme_id=bid,
        desc=_as_str(v.get("title")),
        author=_as_str(owner.get("name")),
        author_id=_as_str(owner.get("mid")),
        create_time=_as_int(v.get("pubdate"), 0),
        duration=_as_int(v.get("duration"), 0) * 1000,   # B站给秒, 统一成毫秒
        cover_urls=_as_url_list(v.get("pic")),
        platform=PLATFORM_BILIBILI,
    )
    # 分 P 信息挂 raw 里, 下载时要用第一 P 的 cid
    pages = v.get("pages")
    if isinstance(pages, list) and pages:
        first = pages[0] if isinstance(pages[0], dict) else {}
        info.raw = {"view": v, "cid": _as_int(first.get("cid"), 0),
                    "page": _as_int(first.get("page"), 1),
                    "part": _as_str(first.get("part"))}
    else:
        info.raw = {"view": v, "cid": _as_int(v.get("cid"), 0)}
    return info


def _dash_tracks(dash: Any, key: str) -> list[dict]:
    if not isinstance(dash, dict):
        return []
    items = dash.get(key)
    if not isinstance(items, list):
        return []
    return [t for t in items if isinstance(t, dict)]


def _track_urls(track: dict) -> list[str]:
    """dash 轨道的 baseUrl + backupUrl。B 站两个字段都可能用驼峰或下划线。"""
    urls: list[str] = []
    base = (track.get("baseUrl") or track.get("base_url")
            or track.get("baseURL") or "")
    if isinstance(base, str) and base.startswith("http"):
        urls.append(base)
    for k in ("backupUrl", "backup_url", "backupURL"):
        urls.extend(_as_url_list(track.get(k)))
    seen = set()
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _audio_penalty(track: dict) -> int:
    """
    音频轨打分: 数字越小越好。
    B 站的 30280/30232/30216 是普通 AAC 档位, 30250/30251 是杜比/无损,
    后者体积大但音质未必人人想要; 这里优先普通档里码率最高的那条。
    """
    tid = _as_int(track.get("id"), 0)
    if tid in (30250, 30251):       # 杜比 / Hi-Res
        return 1
    return 0


def best_audio(dash: Any) -> tuple[str, dict]:
    """挑一条最优音频轨, 返回 (url, track)。

    排序: 先按档位(普通 AAC 优于杜比/Hi-Res), 再按码率。
    不能只按码率 —— 杜比的码率通常最高, 但很多播放器放不出,
    而且体积明显更大; 用户要的是"能正常播放的声音"。
    """
    auds = _dash_tracks(dash, "audio")
    if not auds:
        # 老视频没有独立音轨, 但 dash 里可能带 flac
        auds = _dash_tracks(dash, "flac")
    if not auds:
        return "", {}
    best = max(auds, key=lambda t: (-_audio_penalty(t),
                                    _as_int(t.get("bandwidth"), 0)))
    urls = _track_urls(best)
    return (urls[0] if urls else ""), best


def attach_dash(info: VideoInfo, dash: Any) -> VideoInfo:
    """
    把 playurl 响应里的 DASH 轨变成 `info.candidates`。

    每个视频轨生成一个 Candidate，并把**共用的最优音频 URL** 挂在
    `candidate.audio_url` 上 —— 下载时要下两路再合并。
    同分辨率下有多个 codec（avc/hevc/av1），按码率取最高的那个，
    因为 `Candidate.score()` 已经是 (面积, 码率, 体积)，同分辨率自然选高码率。
    """
    vids = _dash_tracks(dash, "video")
    if not vids:
        return info
    a_url, a_track = best_audio(dash)
    a_bw = _as_int(a_track.get("bandwidth"), 0)

    out: list[Candidate] = []
    seen: set[tuple[int, int, int]] = set()
    for t in vids:
        w = _as_int(t.get("width"), 0)
        h = _as_int(t.get("height"), 0)
        bw = _as_int(t.get("bandwidth"), 0)
        if not (w and h):
            continue
        urls = _track_urls(t)
        if not urls:
            continue
        qid = _as_int(t.get("id"), 0)
        key = (w, h, bw)
        if key in seen:
            continue
        seen.add(key)
        codecid = _as_int(t.get("codecid"), 0)
        codec = {7: "avc", 12: "hevc", 13: "av1"}.get(codecid, str(codecid))
        out.append(
            Candidate(
                url=urls[0],
                width=w,
                height=h,
                bitrate=bw,
                size=0,
                gear_name=QUALITY_NAMES.get(qid, str(qid)),
                quality_key=codec,
                audio_url=a_url,
                audio_bitrate=a_bw,
                alt_urls=urls[1:],
            )
        )
    if out:
        info.candidates = out
    return info


def parse_bili_wrapped(obj: Any) -> VideoInfo:
    """解析 watcher 递过来的 B 站包装对象 `{"_platform":"bilibili", "_view": {...}}`。"""
    o = obj if isinstance(obj, dict) else {}
    view = o.get("_view") if isinstance(o.get("_view"), dict) else o
    bvid = _as_str(o.get("aweme_id")) or _as_str(o.get("bvid"))
    info = parse_bili_view(view, bvid)
    if isinstance(o.get("_dash"), dict):
        attach_dash(info, o["_dash"])
    return info


# --------------------------------------------------------------------------
# 网络：拿 view / playurl
# --------------------------------------------------------------------------

def _api_get(session: requests.Session, url: str, params: dict,
             timeout: int = 15) -> dict:
    """
    请求 B站 API。

    必须显式覆盖 Referer —— 实测抖音的 `https://www.douyin.com/` 会被 B站
    防盗链挡掉(playurl 直接 403 / 返回非 JSON), B站自己的 Referer 或不带
    都能通。session 可能是全局共用的那个(Referer 是抖音), 所以这里逐请求覆盖。
    """
    r = session.get(url, params=params, timeout=timeout,
                    headers={"Referer": REFERER_BILI,
                             "Origin": "https://www.bilibili.com"})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        return {}
    return data


def fetch_view(session: requests.Session, bvid: str,
               log: Callable[[str], None] = print) -> dict:
    """拿视频基本信息（含 cid）。失败返回 {}。"""
    params = {"bvid": bvid} if bvid.upper().startswith("BV") else {"aid": bvid[2:]}
    try:
        d = _api_get(session, API_VIEW, params)
    except Exception as e:                     # noqa: BLE001
        log(f"    ! B站 view 请求失败: {str(e)[:80]}")
        return {}
    if _as_int(d.get("code"), -1) != 0:
        log(f"    ! B站 view 返回 {d.get('code')}: {_as_str(d.get('message'))[:60]}")
        return {}
    data = d.get("data")
    return data if isinstance(data, dict) else {}


def fetch_dash(session: requests.Session, bvid: str, cid: int,
               log: Callable[[str], None] = print) -> dict:
    """
    拿 DASH 播放信息。永远请求最高档 `qn=127` + `fnval=4048` + `fourk=1`，
    实际能拿到哪一档由 B 站的登录态决定。
    """
    params = {"bvid": bvid, "cid": cid, "qn": 127, "fnval": 4048, "fourk": 1}
    if not bvid.upper().startswith("BV"):
        params.pop("bvid", None)
        params["avid"] = bvid[2:]
    try:
        d = _api_get(session, API_PLAYURL, params, timeout=20)
    except Exception as e:                     # noqa: BLE001
        log(f"    ! B站 playurl 请求失败: {str(e)[:80]}")
        return {}
    if _as_int(d.get("code"), -1) != 0:
        log(f"    ! B站 playurl 返回 {d.get('code')}: {_as_str(d.get('message'))[:60]}")
        return {}
    data = d.get("data")
    if not isinstance(data, dict):
        return {}
    dash = data.get("dash")
    if not isinstance(dash, dict):
        # 极老的视频只有 durl（音视频合一的 mp4 分段）
        durl = data.get("durl")
        if isinstance(durl, list) and durl:
            fake = {"_durl": durl, "duration": _as_int(data.get("timelength"), 0)}
            return fake
        return {}
    return dash


def dash_to_durl_candidates(dash: dict) -> list[Candidate]:
    """老视频只有 durl 时，把它当单流候选（不需要合并）。"""
    out: list[Candidate] = []
    durl = dash.get("_durl") if isinstance(dash, dict) else None
    if not isinstance(durl, list):
        return out
    for i, seg in enumerate(durl):
        if not isinstance(seg, dict):
            continue
        urls = _as_url_list(seg.get("url"))
        urls.extend(_as_url_list(seg.get("backup_url")))
        if not urls:
            continue
        out.append(Candidate(url=urls[0], size=_as_int(seg.get("size"), 0),
                             gear_name=f"durl-{i + 1}", alt_urls=urls[1:]))
    return out


# --------------------------------------------------------------------------
# 下载：两路流 + ffmpeg 合并
# --------------------------------------------------------------------------

_FFMPEG_CACHE: list[str] = []


def find_ffmpeg() -> str:
    """
    找 ffmpeg。优先 PATH，其次 imageio-ffmpeg 自带的那个二进制。
    找不到返回空串（调用方降级为「只下视频轨、没有声音」并明确告警）。
    """
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE[0]
    import shutil as _sh
    exe = _sh.which("ffmpeg") or ""
    if not exe:
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe() or ""
        except Exception:                      # noqa: BLE001
            exe = ""
    if exe and Path(exe).exists():
        _FFMPEG_CACHE.append(exe)
        return exe
    _FFMPEG_CACHE.append("")
    return ""


def _download_raw(session: requests.Session, urls: list[str], dest: Path,
                  chunk: int = 1 << 20, retries: int = 3,
                  log: Callable[[str], None] = print,
                  on_progress: Callable[[int, int], None] | None = None) -> bool:
    """
    把一个流下载到 dest。urls 里逐个试(备用域名可能失效)。

    必须显式带 B站 Referer —— 实测抖音的 Referer 或不带 Referer 都会被
    CDN 挡成 403(返回 text/html), 只有 `https://www.bilibili.com/` 才给 206。
    这和 API 侧的规则正好相反(API 侧不带 Referer 也能通), 别搞混。
    """
    for url in urls:
        if not url:
            continue
        # v2.2: 大文件(B站 4K 轨动辄几百 MB)走分段并发。
        # CDN 通常限的是单连接速率, 不是账号速率 —— 实测同一个 448MB 的
        # 4K 轨, 单连接 1.96 MB/s, 8 路并发 13.37 MB/s(6.83 倍)。
        hdrs = {"Referer": REFERER_BILI, "Origin": "https://www.bilibili.com"}
        if core.download_parallel(session, url, dest, log=log,
                                  on_progress=on_progress, headers=hdrs):
            return True
        # 分段没成功(探不到总长 / 服务端不支持 Range)就走原来的单连接
        for attempt in range(1, retries + 1):
            try:
                with session.get(url, stream=True, timeout=60,
                                 headers={"Referer": REFERER_BILI,
                                          "Origin": "https://www.bilibili.com"}) as r:
                    if r.status_code >= 400:
                        log(f"      ! HTTP {r.status_code}, 换地址")
                        break
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if "text/html" in ctype or "application/json" in ctype:
                        log("      ! 返回的不是视频(网页/JSON), 换地址")
                        break
                    total = _as_int(r.headers.get("Content-Length"), 0)
                    done = 0
                    last = time.monotonic()
                    with open(dest, "wb") as f:
                        for block in r.iter_content(chunk):
                            if not block:
                                continue
                            f.write(block)
                            done += len(block)
                            if on_progress and total:
                                now = time.monotonic()
                                if now - last >= 0.5:
                                    on_progress(done, total)
                                    last = now
                if dest.exists() and dest.stat().st_size > 0:
                    if on_progress and total:
                        on_progress(done, total)
                    return True
            except Exception as e:             # noqa: BLE001
                log(f"      ! 第 {attempt} 次下载失败: {str(e)[:70]}")
                time.sleep(1.0)
    return False


def merge_av(video: Path, audio: Path, dest: Path,
             log: Callable[[str], None] = print) -> bool:
    """用 ffmpeg 把视频轨和音频轨合成 mp4（-c copy，不重新编码）。"""
    ff = find_ffmpeg()
    if not ff:
        log("      ! 找不到 ffmpeg, 无法合并音视频")
        return False
    cmd = [ff, "-y", "-i", str(video), "-i", str(audio),
           "-c", "copy", "-movflags", "+faststart", str(dest)]
    try:
        r = core.run_hidden(cmd, capture_output=True, encoding="utf-8",
                            errors="replace", timeout=600)
    except Exception as e:                     # noqa: BLE001
        log(f"      ! ffmpeg 调用失败: {str(e)[:80]}")
        return False
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-3:]
        log(f"      ! ffmpeg 合并失败({r.returncode}): {' / '.join(tail)[:160]}")
        return False
    return dest.exists() and dest.stat().st_size > 0


def download_dash(session: requests.Session, cand: Candidate, dest: Path,
                  log: Callable[[str], None] = print,
                  on_progress: Callable[[int, int], None] | None = None) -> bool:
    """
    下载一条 B 站候选：视频轨 + 音频轨 → 合并 → dest。

    没有音频轨地址（老视频 durl / 无声视频）时直接单流下载。
    """
    urls = [cand.url] + list(cand.alt_urls or [])
    if not cand.audio_url:
        return _download_raw(session, urls, dest, log=log,
                             on_progress=on_progress)

    tmp = Path(tempfile.mkdtemp(prefix="bili_av_"))
    vf = tmp / "video.m4s"
    af = tmp / "audio.m4s"
    merged = tmp / "merged.mp4"
    try:
        log("      视频轨…")
        if not _download_raw(session, urls, vf, log=log, on_progress=on_progress):
            log("      ! 视频轨下载失败")
            return False
        log("      音频轨…")
        a_urls = [cand.audio_url]
        if not _download_raw(session, a_urls, af, log=log):
            # 没音频也要把画面留下, 至少比整个失败好
            log("      ! 音频轨下载失败, 只保留画面(无声音)")
            try:
                vf.replace(dest)
                return dest.exists() and dest.stat().st_size > 0
            except Exception:                  # noqa: BLE001
                return False
        log("      合并音视频…")
        if not merge_av(vf, af, merged, log=log):
            return False
        merged.replace(dest)
        return dest.exists() and dest.stat().st_size > 0
    finally:
        for p in (vf, af, merged):
            try:
                p.unlink(missing_ok=True)
            except Exception:                  # noqa: BLE001
                pass
        try:
            tmp.rmdir()
        except Exception:                      # noqa: BLE001
            pass


def iter_quality_labels(dash: Any) -> Iterable[str]:
    for t in _dash_tracks(dash, "video"):
        qid = _as_int(t.get("id"), 0)
        yield QUALITY_NAMES.get(qid, str(qid))


def csrf_from_cookies(cookies: Iterable[dict]) -> str:
    """从浏览器 cookie 里取 bili_jct（B 站写接口的 csrf token）。"""
    for c in cookies or []:
        if isinstance(c, dict) and c.get("name") == "bili_jct":
            return _as_str(c.get("value"))
    return ""


__all__ = [
    "PLATFORM_BILIBILI",
    "REFERER_BILI",
    "QUALITY_NAMES",
    "is_bili_id",
    "is_bili_url",
    "bv_from_url",
    "parse_bili_view",
    "parse_bili_wrapped",
    "attach_dash",
    "best_audio",
    "fetch_view",
    "fetch_dash",
    "dash_to_durl_candidates",
    "find_ffmpeg",
    "merge_av",
    "download_dash",
    "csrf_from_cookies",
    "safe_name",
]
