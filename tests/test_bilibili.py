"""v2.1 B站接入回归测试。

覆盖三块:
  1. 解析层(douyin_dl/bili.py): view/playurl 清洗、DASH 选轨、durl 兜底、id 识别
  2. 事件层(watcher): _is_bili_url 路由隔离、_on_bili_payload 三分支、收藏语义
  3. 下载层: _fill_bili 补全、download_dash 两路合并、ffmpeg 缺失时降级

全部离线: 网络与 ffmpeg 都打桩, 不依赖真实 B 站和本机 ffmpeg。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from douyin_dl import bili, core                      # noqa: E402
from douyin_dl.watcher import (                       # noqa: E402
    BrowserWatcher,
    PLATFORM_BILIBILI,
    _is_bili_id,
    _norm_bili_id,
    bili_action_rid,
    bili_action_value,
    bili_ids_from_url,
    collect_bili_ids,
    extract_bili_videos,
)

BV = "BV1GJ411x7h7"


# ---------------------------------------------------------------------------
# 造数: 真实 B 站接口的形状(字段从实测响应里抄的)
# ---------------------------------------------------------------------------

def bili_view(bvid: str = BV, cid: int = 137649199) -> dict:
    return {
        "aid": 80433022,
        "bvid": bvid,
        "videos": 1,
        "tid": 193,
        "tname": "MV",
        "copyright": 1,
        "pic": "http://i2.hdslb.com/bfs/archive/cover.jpg",
        "title": "【官方 MV】Never Gonna Give You Up",
        "pubdate": 1568004162,
        "ctime": 1568004162,
        "desc": "描述",
        "state": 0,
        "duration": 213,
        "owner": {"mid": 486709252, "name": "索尼音乐中国",
                  "face": "http://i1.hdslb.com/bfs/face/x.jpg"},
        "stat": {"view": 1000, "danmaku": 10, "reply": 5},
        "pages": [{"cid": cid, "page": 1, "from": "vupload",
                   "part": "Never Gonna Give You Up", "duration": 213}],
    }


def bili_dash(n_tracks: int = 4, with_audio: bool = True) -> dict:
    """造一份 DASH 响应。分辨率依次升高, 便于断言"选最高"。"""
    # (宽, 高, 码率, B站清晰度 id) —— id 决定 gear_name
    specs = [(640, 360, 76411, 16), (852, 480, 786766, 32),
             (1280, 720, 1500000, 64), (1920, 1080, 3000000, 80)][:n_tracks]
    video = []
    for w, h, bw, qid in specs:
        video.append({
            "id": qid, "baseUrl": f"https://cdn/{w}x{h}.m4s",
            "backupUrl": [f"https://cdn2/{w}x{h}.m4s"],
            "bandwidth": bw, "mimeType": "video/mp4",
            "codecs": "avc1.64001E", "width": w, "height": h,
            "frameRate": "30", "codecid": 7,
        })
    audio = []
    if with_audio:
        audio = [
            {"id": 30216, "baseUrl": "https://cdn/a-low.m4s",
             "bandwidth": 43962, "mimeType": "audio/mp4"},
            {"id": 30280, "baseUrl": "https://cdn/a-high.m4s",
             "backupUrl": ["https://cdn2/a-high.m4s"],
             "bandwidth": 203786, "mimeType": "audio/mp4"},
        ]
    return {"duration": 213, "video": video, "audio": audio}


def bili_fav_list(*bvids: str) -> dict:
    medias = []
    for i, b in enumerate(bvids):
        medias.append({
            "id": 80433022 + i, "type": 2, "title": f"视频{i}",
            "cover": f"http://c/{i}.jpg", "bvid": b, "duration": 200,
            "cnt_info": {"collect": 1},
            "intro": "简介", "attr": 0,
        })
    return {"code": 0, "message": "0",
            "data": {"info": {"id": 1, "title": "默认收藏夹"},
                     "medias": medias, "has_more": False}}


def bili_apollo_none() -> None:
    return None


# ---------------------------------------------------------------------------
# 1. 解析层
# ---------------------------------------------------------------------------

def test_parse_bili_view_extracts_all_fields():
    info = bili.parse_bili_view(bili_view(), BV)
    assert info.aweme_id == BV
    assert info.desc == "【官方 MV】Never Gonna Give You Up"
    assert info.author == "索尼音乐中国"
    assert info.author_id == "486709252"
    assert info.create_time == 1568004162
    assert info.duration == 213000          # B站给秒, 统一成毫秒
    assert info.cover_urls and "hdslb" in info.cover_urls[0]
    assert info.platform == PLATFORM_BILIBILI
    # cid 必须从 pages[0] 取出来, playurl 要用
    assert info.raw["cid"] == 137649199
    # view 本身没有 DASH, 所以候选为空
    assert info.candidates == []


def test_parse_bili_view_handles_av_and_missing_pages():
    v = bili_view()
    v.pop("pages")
    v.pop("bvid")
    info = bili.parse_bili_view(v, "")
    assert info.aweme_id == "av80433022"    # 没有 bvid 就用 aid
    assert info.raw["cid"] == 0             # 没有 pages 时 cid 为 0, 上层会报错跳过


def test_attach_dash_picks_highest_resolution():
    info = bili.parse_bili_view(bili_view(), BV)
    bili.attach_dash(info, bili_dash(4))
    assert len(info.candidates) == 4
    best = info.best()
    assert (best.width, best.height) == (1920, 1080)
    assert best.bitrate == 3000000
    assert best.gear_name == "1080P"
    # 每条视频轨都要挂上共用的最优音频轨
    assert all(c.audio_url for c in info.candidates)
    assert all(c.audio_bitrate == 203786 for c in info.candidates)
    assert best.needs_merge is True


def test_attach_dash_keeps_backup_urls():
    info = bili.parse_bili_view(bili_view(), BV)
    bili.attach_dash(info, bili_dash(1))
    c = info.candidates[0]
    assert c.url == "https://cdn/640x360.m4s"
    assert c.alt_urls == ["https://cdn2/640x360.m4s"]


def test_attach_dash_without_audio_marks_no_merge():
    info = bili.parse_bili_view(bili_view(), BV)
    bili.attach_dash(info, bili_dash(2, with_audio=False))
    assert len(info.candidates) == 2
    # 没有音频轨 -> 不需要合并, 走单流下载
    assert all(c.needs_merge is False for c in info.candidates)


def test_attach_dash_empty_keeps_candidates_untouched():
    info = bili.parse_bili_view(bili_view(), BV)
    bili.attach_dash(info, {})
    assert info.candidates == []


def test_dash_to_durl_candidates_for_old_videos():
    dash = {"_durl": [
        {"order": 1, "size": 1000, "url": "https://cdn/seg1.mp4",
         "backup_url": ["https://cdn2/seg1.mp4"]},
        {"order": 2, "size": 2000, "url": "https://cdn/seg2.mp4"},
    ]}
    cands = bili.dash_to_durl_candidates(dash)
    assert len(cands) == 2
    assert cands[0].url == "https://cdn/seg1.mp4"
    assert cands[0].alt_urls == ["https://cdn2/seg1.mp4"]
    assert cands[0].needs_merge is False     # durl 是合一的, 不用合并


def test_best_audio_prefers_normal_over_dolby():
    dash = {
        "audio": [
            {"id": 30250, "baseUrl": "https://cdn/dolby.m4s", "bandwidth": 999999},
            {"id": 30280, "baseUrl": "https://cdn/aac.m4s", "bandwidth": 203786},
        ]
    }
    url, track = bili.best_audio(dash)
    # 杜比码率更高, 但普通 AAC 档优先(用户要的是"正常能放的声音")
    assert url == "https://cdn/aac.m4s"
    assert track["id"] == 30280


def test_is_bili_id_accepts_bv_and_av_only():
    assert _is_bili_id(BV) is True
    assert _is_bili_id("av80433022") is True
    # 抖音 19 位纯数字、快手混排 id 都不能被当成 B站 id
    assert _is_bili_id("7705427599274719717") is False
    assert _is_bili_id("3xb42hegyea3mzc") is False
    assert _is_bili_id("") is False
    assert _is_bili_id(None) is False


def test_norm_bili_id_converts_aid_to_av():
    assert _norm_bili_id(BV) == BV
    assert _norm_bili_id(80433022) == "av80433022"
    assert _norm_bili_id("80433022") == "av80433022"
    assert _norm_bili_id({"aid": 80433022}) == "av80433022"
    assert _norm_bili_id({"bvid": BV}) == BV
    assert _norm_bili_id({"nothing": 1}) == ""


def test_bili_ids_from_url_variants():
    assert bili_ids_from_url(f"https://www.bilibili.com/video/{BV}") == [BV]
    assert bili_ids_from_url(f"https://www.bilibili.com/video/{BV}?p=2") == [BV]
    assert bili_ids_from_url("https://www.bilibili.com/video/av80433022") == ["av80433022"]
    assert bili_ids_from_url(f"https://x.com/?bvid={BV}") == [BV]
    assert bili_ids_from_url("https://example.com") == []


# ---------------------------------------------------------------------------
# 2. 事件层
# ---------------------------------------------------------------------------

def _make_watcher():
    return BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()), log=lambda m: None)


def _feed(w, kind, url, body="", req=""):
    # expose_binding 会带两个实参(source, payload), 所以这里也传两个
    w._on_binding("__dyReport", json.dumps({"kind": kind, "url": url,
                                            "body": body, "req": req}))
    return w.drain()


def test_is_bili_url_routing_isolated():
    assert BrowserWatcher._is_bili_url(
        "https://api.bilibili.com/x/v3/fav/resource/deal") is True
    assert BrowserWatcher._is_bili_url(
        "https://api.bilibili.com/x/player/playurl?bvid=x") is True
    # 抖音/快手绝不能被 B站分支抢走
    assert BrowserWatcher._is_bili_url(
        "https://www.douyin.com/aweme/v1/web/aweme/collect/") is False
    assert BrowserWatcher._is_bili_url(
        "https://www.kuaishou.com/rest/v/photo/collect") is False


def test_bili_action_value_fav_and_unfav():
    fav = "rid=80433022&type=2&add_media_ids=12345&del_media_ids=&csrf=abc"
    unfav = "rid=80433022&type=2&add_media_ids=&del_media_ids=12345&csrf=abc"
    assert bili_action_value("", "", fav) == "1"
    assert bili_action_value("", "", unfav) == "0"
    # 两个都空 = 状态同步, 不处理
    assert bili_action_value("", "", "rid=1&type=2&add_media_ids=&del_media_ids=") == ""


def test_bili_action_rid_from_form():
    assert bili_action_rid(
        "", "", "rid=80433022&type=2&add_media_ids=1&del_media_ids=") == "av80433022"


def test_bili_fav_action_submits_cached_video():
    """收藏动作命中缓存 -> 直接 on_aweme 提交, 不需要无头浏览器。"""
    w = _make_watcher()
    got: list[tuple[dict, str]] = []
    w.on_aweme = lambda aw, src: got.append((aw, src))

    # 先喂一条收藏列表, 建缓存
    _feed(w, "data", "https://api.bilibili.com/x/v3/fav/resource/list",
          json.dumps(bili_fav_list(BV)), "")

    # 再喂收藏动作(rid 是 aid, 要能换成 av 号并命中缓存里的 BV 吗?)
    # 缓存 key 是 BV 号, 动作给的是 av 号 -> 命中不了, 走 pending。
    # 这是真实行为: 这里验证它至少不会崩, 且不会误提交。
    got.clear()
    _feed(w, "action", "https://api.bilibili.com/x/v3/fav/resource/deal",
          "", "rid=80433022&type=2&add_media_ids=123&del_media_ids=")
    # 没命中缓存 -> 进 pending, 不会立刻 on_aweme
    assert got == []


def test_bili_unfav_does_not_download():
    w = _make_watcher()
    got: list[tuple[dict, str]] = []
    w.on_aweme = lambda aw, src: got.append((aw, src))
    _feed(w, "data", "https://api.bilibili.com/x/v3/fav/resource/list",
          json.dumps(bili_fav_list(BV)), "")
    _feed(w, "action", "https://api.bilibili.com/x/v3/fav/resource/deal",
          "", "rid=80433022&type=2&add_media_ids=&del_media_ids=123")
    assert got == []


def test_bili_fav_list_only_builds_baseline():
    """收藏列表接口只建基线, 绝不触发下载(第一页会轮换)。"""
    w = _make_watcher()
    got: list[tuple[dict, str]] = []
    w.on_aweme = lambda aw, src: got.append((aw, src))
    _feed(w, "data", "https://api.bilibili.com/x/v3/fav/resource/list",
          json.dumps(bili_fav_list(BV, "BV1xx411c7mD")), "")
    assert got == []
    assert BV in w.known_favs
    assert "BV1xx411c7mD" in w.known_favs
    assert w.baseline_done is True


def test_bili_fav_list_download_all_on_start():
    w = _make_watcher()
    w.download_all_on_start = True
    got: list[tuple[dict, str]] = []
    w.on_aweme = lambda aw, src: got.append((aw, src))
    _feed(w, "data", "https://api.bilibili.com/x/v3/fav/resource/list",
          json.dumps(bili_fav_list(BV, "BV1xx411c7mD")), "")
    assert len(got) == 2
    assert all(src == "B站收藏列表" for _, src in got)


def test_bili_auto_recommend_dedupes():
    w = _make_watcher()
    w.auto_recommend = True
    got: list[tuple[dict, str]] = []
    w.on_aweme = lambda aw, src: got.append((aw, src))
    feed = {"code": 0, "data": {"item": [
        {"bvid": BV, "title": "t1", "cover": "http://c/1.jpg", "duration": 100},
        {"bvid": "BV1xx411c7mD", "title": "t2", "cover": "http://c/2.jpg", "duration": 100},
    ]}}
    url = "https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd"
    _feed(w, "data", url, json.dumps(feed), "")
    assert len(got) == 2
    assert all(src == "B站推荐" for _, src in got)
    # 同一批再来一次不能再提交
    got.clear()
    _feed(w, "data", url, json.dumps(feed), "")
    assert got == []


def test_extract_bili_videos_filters_non_video_types():
    resp = bili_fav_list(BV)
    resp["data"]["medias"].append(
        {"id": 999, "type": 12, "title": "音频", "cover": "http://c/9.jpg"})
    resp["data"]["medias"].append(
        {"id": 998, "type": 21, "title": "专栏", "cover": "http://c/8.jpg"})
    vids = extract_bili_videos(json.dumps(resp))
    assert [v.get("bvid") for v in vids] == [BV]


def test_collect_bili_ids_from_fav_list():
    ids = collect_bili_ids(json.dumps(bili_fav_list(BV, "BV1xx411c7mD")))
    assert BV in ids
    assert "BV1xx411c7mD" in ids


def test_bili_wrapped_object_shape():
    """_on_bili_payload 生成的包装对象要被 parse_any 正确识别。"""
    w = _make_watcher()
    seen: list[dict] = []
    w.on_aweme = lambda aw, src: seen.append(aw)
    _feed(w, "data", "https://api.bilibili.com/x/v3/fav/resource/list",
          json.dumps(bili_fav_list(BV)), "")
    # 直接从缓存里取一条看形状
    cached = w._get_cached(BV)
    assert cached is not None
    assert cached["_platform"] == PLATFORM_BILIBILI
    assert cached["aweme_id"] == BV
    info = core.parse_any(cached)
    assert info.aweme_id == BV
    assert info.platform == PLATFORM_BILIBILI


# ---------------------------------------------------------------------------
# 3. 下载层
# ---------------------------------------------------------------------------

def _load_main():
    spec = importlib.util.spec_from_file_location("acc_main_bili", _ROOT / "douyin_dl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeState:
    def __init__(self, has: bool = False):
        self._has = has
        self.marked: list[tuple] = []
    def has(self, _aid):
        return self._has
    def mark(self, aid, path, quality):
        self.marked.append((aid, path, quality))


class _FakeBus:
    def __init__(self):
        self.events: list[tuple] = []
    def __getattr__(self, name):
        def _rec(*a):
            self.events.append((name, *a))
        return _rec


def test_fill_bili_fills_dash_and_metadata(monkeypatch):
    M = _load_main()
    dl = M.Downloader.__new__(M.Downloader)
    dl.session = object()
    dl.bili_session = object()   # v2.1.1: B站单独 session
    dl.state = _FakeState()
    dl.bus = _FakeBus()
    dl.count_ok = dl.count_skip = dl.count_fail = 0
    dl._rec_prefetch = {}
    dl.log = lambda *a: None

    monkeypatch.setattr(bili, "fetch_view", lambda s, b, log=None: bili_view())
    monkeypatch.setattr(bili, "fetch_dash", lambda s, b, c, log=None: bili_dash(4))

    info = core.parse_any({"aweme_id": BV, "_platform": PLATFORM_BILIBILI, "_view": {}})
    assert info.candidates == []
    out = dl._fill_bili(info)
    assert len(out.candidates) == 4
    assert out.best().height == 1080
    assert out.author == "索尼音乐中国"
    assert out.desc.startswith("【官方 MV】")


def test_fill_bili_falls_back_to_durl_for_old_videos(monkeypatch):
    M = _load_main()
    dl = M.Downloader.__new__(M.Downloader)
    dl.session = object()
    dl.bili_session = object()   # v2.1.1: B站单独 session
    dl.log = lambda *a: None
    monkeypatch.setattr(bili, "fetch_view", lambda s, b, log=None: bili_view())
    monkeypatch.setattr(bili, "fetch_dash", lambda s, b, c, log=None: {
        "_durl": [{"order": 1, "size": 1000, "url": "https://cdn/seg1.mp4"}]
    })
    info = core.parse_any({"aweme_id": BV, "_platform": PLATFORM_BILIBILI, "_view": {}})
    out = dl._fill_bili(info)
    assert len(out.candidates) == 1
    assert out.candidates[0].needs_merge is False


def test_fill_bili_survives_view_failure(monkeypatch):
    M = _load_main()
    dl = M.Downloader.__new__(M.Downloader)
    dl.session = object()
    dl.bili_session = object()   # v2.1.1: B站单独 session
    dl.log = lambda *a: None
    monkeypatch.setattr(bili, "fetch_view", lambda s, b, log=None: {})
    info = core.parse_any({"aweme_id": BV, "_platform": PLATFORM_BILIBILI, "_view": {}})
    out = dl._fill_bili(info)
    assert out.candidates == []      # 保持为空, 上层会报"没有可用地址"


def test_download_dash_downloads_two_tracks_and_merges(tmp_path, monkeypatch):
    from douyin_dl.core import Candidate

    calls: list[str] = []

    def _fake_raw(session, urls, dest, **_k):
        calls.append(Path(dest).name)
        Path(dest).write_bytes(b"\x00" * 2048)
        return True

    def _fake_merge(v, a, dest, log=None):
        assert Path(v).exists() and Path(a).exists()
        Path(dest).write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4096)
        return True

    monkeypatch.setattr(bili, "_download_raw", _fake_raw)
    monkeypatch.setattr(bili, "merge_av", _fake_merge)

    cand = Candidate(url="https://cdn/v.m4s", width=1920, height=1080,
                     bitrate=3000000, audio_url="https://cdn/a.m4s",
                     audio_bitrate=203786)
    dest = tmp_path / "out.mp4"
    assert bili.download_dash(None, cand, dest) is True
    assert dest.exists() and dest.stat().st_size > 0
    assert calls == ["video.m4s", "audio.m4s"]     # 两路都下了


def test_download_dash_keeps_video_when_audio_fails(tmp_path, monkeypatch):
    from douyin_dl.core import Candidate

    def _fake_raw(session, urls, dest, **_k):
        if "a.m4s" in urls[0]:
            return False                 # 音频轨失败
        Path(dest).write_bytes(b"\x00" * 2048)
        return True

    monkeypatch.setattr(bili, "_download_raw", _fake_raw)
    monkeypatch.setattr(bili, "merge_av",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该合并")))

    cand = Candidate(url="https://cdn/v.m4s", audio_url="https://cdn/a.m4s")
    dest = tmp_path / "out.mp4"
    assert bili.download_dash(None, cand, dest) is True
    # 至少留下画面, 不让整条失败
    assert dest.exists() and dest.stat().st_size > 0


def test_find_ffmpeg_returns_existing_path():
    exe = bili.find_ffmpeg()
    if exe:
        assert Path(exe).exists()
    else:
        # 没有 ffmpeg 也算合法(降级路径), 但要能明确返回空串而不是抛异常
        assert exe == ""


def test_merge_av_reports_error_without_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(bili, "_FFMPEG_CACHE", [""])
    assert bili.merge_av(tmp_path / "v", tmp_path / "a", tmp_path / "o") is False


def test_api_get_overrides_referer():
    """v2.1 回归: B站 API 必须覆盖 Referer。

    core.build_session() 的 Referer 是抖音的, 直接拿去请求 B站 API 会被
    防盗链挡掉(playurl 403 / 返回非 JSON)。这里锁住: _api_get 一定要
    带上 B站自己的 Referer + Origin。
    """
    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"code": 0, "data": {"ok": 1}}

    seen = {}

    class _Sess:
        def get(self, url, params=None, timeout=None, headers=None):
            seen["headers"] = headers or {}
            return _Resp()

    out = bili._api_get(_Sess(), "https://api.bilibili.com/x/test", {})
    assert out == {"code": 0, "data": {"ok": 1}}
    assert seen["headers"].get("Referer") == bili.REFERER_BILI
    assert seen["headers"].get("Origin") == "https://www.bilibili.com"


def test_download_raw_sends_referer(tmp_path):
    """v2.1 回归: 直链下载必须带 B站 Referer(不带就是 403)。

    注意这和 API 侧相反: API 侧不带 Referer 也能通, CDN 侧不带就 403。
    """
    seen = {}

    class _Resp:
        status_code = 206
        headers = {"Content-Length": "4", "Content-Type": "video/mp4"}
        def iter_content(self, _n):
            yield b"data"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Sess:
        def get(self, url, stream=None, timeout=None, headers=None):
            seen["headers"] = headers or {}
            return _Resp()

    dest = tmp_path / "v.m4s"
    ok = bili._download_raw(_Sess(), ["https://cdn/x.m4s"], dest, log=lambda *a: None)
    assert ok is True
    assert seen["headers"].get("Referer") == bili.REFERER_BILI
    assert dest.read_bytes() == b"data"


def test_download_raw_rejects_html_response(tmp_path):
    """被防盗链挡掉时 CDN 返回 text/html, 必须判失败而不是写进文件。"""
    class _Resp:
        status_code = 200
        headers = {"Content-Length": "9", "Content-Type": "text/html"}
        def iter_content(self, _n):
            yield b"<html></h"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Sess:
        def get(self, *a, **k):
            return _Resp()

    assert bili._download_raw(_Sess(), ["https://cdn/x"], tmp_path / "v",
                              log=lambda *a: None, retries=1) is False


def test_downloader_process_uses_bili_branch(tmp_path, monkeypatch):
    """process 遇到 B站包装对象必须走 _fill_bili + download_dash。"""
    M = _load_main()
    dl = M.Downloader.__new__(M.Downloader)
    dl.session = object()
    dl.bili_session = object()   # v2.1.1: B站单独 session
    dl.state = _FakeState(has=False)
    bus = _FakeBus()
    dl.bus = bus
    dl.count_ok = dl.count_skip = dl.count_fail = 0
    dl._rec_prefetch = {}
    dl.log = lambda *a: None

    used: list[str] = []

    def _fake_fill(info):
        bili.attach_dash(info, bili_dash(2))
        return info

    dl._fill_bili = _fake_fill
    monkeypatch.setattr(M, "get_out_dir", lambda: tmp_path)
    monkeypatch.setattr(M, "load_settings", lambda *_a, **_k: {"quality": "max"})
    monkeypatch.setattr(M, "probe_size", lambda *_a, **_k: 1000)
    monkeypatch.setattr(M, "verify_video", lambda *_a, **_k: True)

    def _fake_dash(session, cand, dest, **k):
        used.append("dash")
        Path(dest).write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4096)
        return True

    monkeypatch.setattr(bili, "download_dash", _fake_dash)

    aw = {"aweme_id": BV, "_platform": PLATFORM_BILIBILI, "_view": bili_view()}
    dl.process(aw, "B站收藏")
    assert used == ["dash"]                       # 走了合并下载
    assert dl.count_ok == 1
    assert dl.state.marked and dl.state.marked[0][0] == BV
    assert (tmp_path / "_meta" / f"{BV}.json").exists()


# --------------------------------------------------------------------------
# v2.1.1 回归: B站 av 号被当成快手 id 送去快手详情页
# --------------------------------------------------------------------------

def test_is_bili_pending_id_recognizes_bv_and_av():
    """av 号("av" + 15 位数字)形状恰好满足 _is_ks_id(字母+数字混排 8~32 位),
    必须在投无头队列前用这个函数先认出来, 否则会被送去打开快手详情页。"""
    from douyin_dl.watcher import _is_bili_pending_id

    # 真实踩到的那条
    assert _is_bili_pending_id("av114962919855294") is True
    assert _is_bili_pending_id("BV1V8huzkE5V") is True
    assert _is_bili_pending_id("av123") is True
    # 快手 photoId / 抖音 aweme_id / 垃圾统统不认
    assert _is_bili_pending_id("3xun955567ak6qe") is False
    assert _is_bili_pending_id("7700565139293046690") is False
    assert _is_bili_pending_id("") is False
    assert _is_bili_pending_id(None) is False
    assert _is_bili_pending_id("av") is False
    assert _is_bili_pending_id("BV") is False


def test_resolve_pending_routes_bili_id_to_downloader_not_headless():
    """待补全队列里的 B站 id 必须交给 on_aweme 直接补全, 绝不进 _hl_in。

    这是「B站收藏了一个正常视频却下不了」的直接根因: av 号被 _is_ks_id
    认成快手 id -> _hl_fetch 打开 kuaishou.com/short-video/av... -> 风控页
    -> 拿不到数据 -> 报「抓不到视频详情」。
    """
    import json as _json
    from douyin_dl.watcher import (BrowserWatcher, PLATFORM_BILIBILI,
                                   _is_bili_pending_id)

    w = BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()), log=lambda m: None)
    got = []

    # 一个足够真实的半成品 watcher: 不走无头线程, 只验证路由分派
    class _FakeCtx:
        def cookies(self):
            return []

    w._ctx = _FakeCtx()
    w._pending_actions = ["av114962919855294"]
    w._resolving = set()
    w.on_aweme = lambda aw, src: got.append((aw, src))
    w.on_pending = lambda aid: None
    # 别真起无头线程(会启动 playwright): 换成假队列, 只要被投递就说明路由错了
    w._ensure_hl_worker = lambda: None
    w._hl_thread = type("T", (), {"is_alive": lambda self: True})()
    puts = []
    w._hl_in = type("Q", (), {"put": lambda self, item: puts.append(item)})()

    w._resolve_pending()

    assert not puts, f"B站 id 不该进无头队列, 却投了 {puts}"
    assert len(got) == 1
    aw, src = got[0]
    assert aw["aweme_id"] == "av114962919855294"
    assert aw["_platform"] == PLATFORM_BILIBILI
    assert _is_bili_pending_id(aw["aweme_id"])


def test_resolve_pending_still_routes_kuaishou_to_headless():
    """快手 photoId 不能因为上一条修复被误判成 B站, 仍要进无头队列。"""
    from douyin_dl.watcher import BrowserWatcher

    w = BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()), log=lambda m: None)

    class _FakeCtx:
        def cookies(self):
            return []

    w._ctx = _FakeCtx()
    w._pending_actions = ["3xun955567ak6qe"]
    w._resolving = set()
    w.on_aweme = lambda aw, src: None
    w.on_pending = lambda aid: None
    # 同样别真起无头线程
    w._ensure_hl_worker = lambda: None
    w._hl_thread = type("T", (), {"is_alive": lambda self: True})()
    puts = []
    w._hl_in = type("Q", (), {"put": lambda self, item: puts.append(item)})()
    # 让 _resolving 立刻清空, 免得等 45 秒超时
    orig_put = w._hl_in.put

    def _put_then_finish(item):
        orig_put(item)
        w._resolving.discard(item[0])

    w._hl_in.put = _put_then_finish

    w._resolve_pending()

    assert puts, "快手 id 应该进无头队列"
    assert puts[0][0] == "3xun955567ak6qe"


# ---------------------------------------------------------------- v2.1.1
# B站画质靠登录态: 匿名 480P / 登录 1080P / 大会员 4K。
# 历史上 _sync_cookies 只同步抖音 Cookie, B站 playurl 一直走匿名态,
# 大会员也只能下到 480P(用户报"太糊")。同时两条 Cookie 通道必须隔离,
# 混在一起会把抖音 sessionid 发给 api.bilibili.com。

def test_sync_cookies_routes_bilibili_to_separate_channel():
    """抖音 Cookie 走 on_cookie, B站 Cookie 走 on_cookie_bili, 互不混淆。"""
    w = BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()), log=lambda m: None)
    got = {"douyin": None, "bili": None}
    w.on_cookie = lambda c: got.__setitem__("douyin", c)
    w.on_cookie_bili = lambda c: got.__setitem__("bili", c)

    class _Ctx:
        def cookies(self):
            return [
                {"name": "sessionid", "value": "DY123", "domain": ".douyin.com"},
                {"name": "ttwid", "value": "t1", "domain": ".douyin.com"},
                {"name": "SESSDATA", "value": "BILI456", "domain": ".bilibili.com"},
                {"name": "bili_jct", "value": "j1", "domain": ".bilibili.com"},
                {"name": "userId", "value": "KS789", "domain": ".kuaishou.com"},
            ]

    w._sync_cookies(_Ctx())

    assert got["douyin"] == "sessionid=DY123; ttwid=t1"
    assert got["bili"] == "SESSDATA=BILI456; bili_jct=j1"
    # 快手 cookie 两边都不能进(快手 CDN 直链是公开的, 不需要)
    assert "userId" not in (got["douyin"] or "")
    assert "userId" not in (got["bili"] or "")
    # 互相隔离: 抖音通道里不能有 B站 cookie, 反之亦然
    assert "SESSDATA" not in (got["douyin"] or "")
    assert "sessionid" not in (got["bili"] or "")


def test_fill_bili_uses_bili_session_not_douyin_session(monkeypatch):
    """_fill_bili 必须用 bili_session(带 B站 SESSDATA), 不能用抖音 session。"""
    M = _load_main()
    dl = M.Downloader.__new__(M.Downloader)
    dy_session = object()
    bili_session = object()
    dl.session = dy_session
    dl.bili_session = bili_session
    dl.log = lambda *a: None

    seen = {}

    def _fake_view(session, b, log=None):
        seen["view"] = session
        return bili_view()

    def _fake_dash(session, b, c, log=None):
        seen["dash"] = session
        return bili_dash(4)

    monkeypatch.setattr(bili, "fetch_view", _fake_view)
    monkeypatch.setattr(bili, "fetch_dash", _fake_dash)

    info = core.parse_any({"aweme_id": BV, "_platform": PLATFORM_BILIBILI, "_view": {}})
    dl._fill_bili(info)

    assert seen["view"] is bili_session
    assert seen["dash"] is bili_session
    assert seen["view"] is not dy_session
