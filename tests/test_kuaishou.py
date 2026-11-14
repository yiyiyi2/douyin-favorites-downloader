# -*- coding: utf-8 -*-
"""v1.7 快手接入回归测试。

全部离线: 用真实响应形状构造的样例, 不起浏览器、不联网。
覆盖三条链: 解析(REST + Apollo) → 事件分流(收藏/取消/列表基线/推荐页) → 下载接线。
"""
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from douyin_dl import core
from douyin_dl.watcher import (
    BrowserWatcher,
    PLATFORM_KUAISHOU,
    _is_ks_id,
)
from douyin_dl.ui import _safe_id

_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 造数据: 完全照 _ks_rest_v_feed_hot.json 的真实形状
# ---------------------------------------------------------------------------
def ks_photo(pid="3xb42hegyea3mzc", w=960, h=720, br=2006):
    return {
        "id": pid,
        "caption": "云野云峰下雨打雷该怎么玩 #光遇",
        "duration": 92066,
        "timestamp": 1789307548496,
        "width": w, "height": h,
        "coverUrl": "https://p23.a.yximgs.com/upic/cover.jpg",
        "liked": False, "collected": False, "viewCount": "19.9万",
        "manifest": {
            "version": "1.0.0", "businessType": 2, "mediaType": 2,
            "adaptationSet": [{
                "id": 1, "duration": 92066,
                "representation": [{
                    "id": 1,
                    "url": f"https://cdn.djvod.ndcimgs.com/{pid}_h264.mp4",
                    "backupUrl": [f"https://v1.kwaicdn.com/{pid}_h264_bk.mp4"],
                    "width": w, "height": h,
                    "avgBitrate": br, "maxBitrate": br + 1300,
                    "fileSize": 23089393,
                    "qualityType": "720p", "qualityLabel": "高清",
                }],
            }],
        },
        "manifestH265": {
            "adaptationSet": [{
                "representation": [{
                    "url": f"https://cdn.djvod.ndcimgs.com/{pid}_h265.mp4",
                    "backupUrl": [f"https://v1.kwaicdn.com/{pid}_h265_bk.mp4"],
                    "width": w, "height": h,
                    "avgBitrate": 917, "maxBitrate": 3000,
                    "fileSize": 10556694,
                    "qualityType": "720p", "qualityLabel": "高清",
                }],
            }],
        },
    }


def ks_feed_body(*photos):
    """真实形状: feeds[i] = {photo:{...}, author:{...}, ...} —— author 与 photo 平级。"""
    return {
        "result": 1, "pcursor": "1", "llsid": "", "host-name": "x",
        "feeds": [
            {"type": 1, "tags": [], "photo": ph,
             "author": {"id": "3x5dun6q86m358s", "name": "义沉沉",
                        "following": False, "headerUrl": ""},
             "danmakuSwitch": {}, "comment": {}}
            for ph in photos
        ],
    }


def ks_apollo_client(pid="3xb42hegyea3mzc"):
    """详情页 window.__APOLLO_STATE__.defaultClient 的形状(type:id / type:json 引用)。"""
    return {
        f"VisionVideoDetailPhoto:{pid}": {
            "id": pid, "caption": "详情页描述", "duration": 92066,
            "timestamp": 1789307548496,
            "coverUrl": "https://p23.a.yximgs.com/c.jpg",
            "photoUrl": f"https://cdn.djvod.ndcimgs.com/{pid}_720.mp4",
            "manifest": {"type": "id", "id": f"$VisionVideoDetailPhoto:{pid}.manifest"},
            "__typename": "VisionVideoDetailPhoto",
        },
        f"$VisionVideoDetailPhoto:{pid}.manifest": {
            "mediaType": 2, "version": "1.0.0",
            "adaptationSet": [{"type": "id", "id": "VisionVideoAdaptationSet:1"}],
        },
        "VisionVideoAdaptationSet:1": {
            "id": 1, "duration": 92066,
            "representation": [{"type": "id", "id": "VisionVideoSetRepresentation:1"}],
        },
        "VisionVideoSetRepresentation:1": {
            "id": 1,
            "url": f"https://cdn.djvod.ndcimgs.com/{pid}_1080.mp4",
            "backupUrl": {"type": "json", "json": [
                f"https://v1.kwaicdn.com/{pid}_1080_bk.mp4"]},
            "width": 1080, "height": 1920,
            "avgBitrate": 5000, "maxBitrate": 6000,
            "fileSize": 40000000,
            "qualityType": "1080p", "qualityLabel": "超清",
        },
        "VisionVideoDetailAuthor:3x5dun6q86m358s": {
            "id": "3x5dun6q86m358s", "name": "义沉沉", "__typename": "VisionVideoDetailAuthor",
        },
    }


# ---------------------------------------------------------------------------
# 1. id 判定 + _safe_id
# ---------------------------------------------------------------------------
def test_is_kuaishou_id():
    assert core.is_kuaishou_id("3xb42hegyea3mzc")
    assert core.is_kuaishou_id("3x-abc_def")
    assert not core.is_kuaishou_id("7705427599274719717")   # 纯数字是抖音
    assert not core.is_kuaishou_id("abc")
    assert not core.is_kuaishou_id("short")
    assert not core.is_kuaishou_id("../etc/passwd")
    assert not core.is_kuaishou_id("a" * 40)
    assert not core.is_kuaishou_id(None)
    assert _is_ks_id("3xb42hegyea3mzc")


def test_safe_id_accepts_kuaishou_but_blocks_traversal():
    assert _safe_id("3xb42hegyea3mzc") == "3xb42hegyea3mzc"
    assert _safe_id("7705427599274719717") == "7705427599274719717"
    assert _safe_id("../etc/passwd") == ""
    assert _safe_id("a.b") == ""
    assert _safe_id("中文") == ""
    assert _safe_id("has space") == ""


# ---------------------------------------------------------------------------
# 2. parse_kuaishou: REST photo / 包装对象 / 最高画质 / 水印过滤 / 兜底
# ---------------------------------------------------------------------------
def test_parse_kuaishou_rest_photo():
    ph = ks_photo()
    info = core.parse_kuaishou(ph)
    assert info.aweme_id == "3xb42hegyea3mzc"
    assert info.desc.startswith("云野云峰")
    assert info.create_time == 1789307548          # 毫秒 → 秒
    assert info.duration == 92066                  # 毫秒, 不换算
    assert info.cover_urls == ["https://p23.a.yximgs.com/upic/cover.jpg"]
    # manifest + manifestH265 各 1 档 × 2 地址(主+备) = 4 个候选
    assert len(info.candidates) == 4
    best = info.best()
    assert (best.width, best.height) == (960, 720)
    assert best.bitrate == 2006                     # H264 平均码率 > H265 的 917
    assert best.size == 23089393
    assert best.gear_name == "高清" and best.quality_key == "720p"
    assert best.url.endswith("_h264.mp4")


def test_parse_kuaishou_picks_highest_resolution():
    ph = ks_photo()
    # 往 H264 那个 adaptationSet 里塞一档 4K, 应该被选中
    ph["manifest"]["adaptationSet"][0]["representation"].append({
        "url": "https://cdn/4k.mp4", "width": 2160, "height": 3840,
        "avgBitrate": 9000, "fileSize": 99000000,
    })
    ph["author"] = {"id": "u1", "name": "作者B"}
    info = core.parse_kuaishou(ph)
    assert info.author == "作者B" and info.author_id == "u1"
    best = info.best()
    assert (best.width, best.height) == (2160, 3840)
    assert best.url == "https://cdn/4k.mp4"


def test_parse_kuaishou_filters_watermarked():
    ph = ks_photo()
    ph["manifest"]["adaptationSet"][0]["representation"][0]["url"] = \
        "https://cdn/playwm_720.mp4"
    info = core.parse_kuaishou(ph)
    assert all("playwm" not in c.url for c in info.candidates)


def test_parse_kuaishou_photo_url_fallback():
    ph = {"id": "3xb42hegyea3mzc", "caption": "只有 photoUrl",
          "photoUrl": "https://cdn/720.mp4", "width": 720, "height": 1280}
    info = core.parse_kuaishou(ph)
    assert len(info.candidates) == 1
    assert info.candidates[0].url == "https://cdn/720.mp4"
    assert info.candidates[0].gear_name == "photoUrl"


def test_parse_any_dispatches():
    ph = ks_photo()
    wrapped = {"aweme_id": ph["id"], "_platform": PLATFORM_KUAISHOU, "_ks": ph}
    i1 = core.parse_any(wrapped)
    assert i1.aweme_id == ph["id"] and len(i1.candidates) == 4
    # 抖音原生对象不受影响
    dy = {"aweme_id": "7300000000000000001", "desc": "抖音",
          "video": {"duration": 1000, "bit_rate": [{
              "play_addr": {"url_list": ["https://v/a.mp4"],
                            "width": 720, "height": 1280}}]}}
    i2 = core.parse_any(dy)
    assert i2.aweme_id == "7300000000000000001" and i2.desc == "抖音"


# ---------------------------------------------------------------------------
# 3. Apollo 详情页缓存: type:id / type:json 解引用
# ---------------------------------------------------------------------------
def test_parse_kuaishou_apollo_deref():
    C = ks_apollo_client()
    raw = C["VisionVideoDetailPhoto:3xb42hegyea3mzc"]
    info = core.parse_kuaishou(raw, C)
    assert info.aweme_id == "3xb42hegyea3mzc"
    best = info.best()
    assert (best.width, best.height) == (1080, 1920)
    assert best.bitrate == 5000
    assert best.url.endswith("_1080.mp4")
    # backupUrl 是 {"type":"json","json":[...]} 也要剥壳收进来
    assert any(c.url.endswith("_1080_bk.mp4") for c in info.candidates)


def test_parse_kuaishou_apollo_author_attached():
    C = ks_apollo_client()
    raw = dict(C["VisionVideoDetailPhoto:3xb42hegyea3mzc"])
    raw["author"] = C["VisionVideoDetailAuthor:3x5dun6q86m358s"]
    info = core.parse_kuaishou(raw, C)
    assert info.author == "义沉沉"
    assert info.author_id == "3x5dun6q86m358s"


# ---------------------------------------------------------------------------
# 4. watcher 事件分流
# ---------------------------------------------------------------------------
def _new_watcher(**kw):
    got, pending, failed = [], [], []
    w = BrowserWatcher(
        profile_dir=Path("."),
        on_aweme=lambda a, s: got.append((a["aweme_id"], s)),
        on_pending=lambda aid: pending.append(aid),
        log=lambda *a, **k: None,
    )
    w.on_fail = lambda aid, why: failed.append((aid, why))
    for k, v in kw.items():
        setattr(w, k, v)
    return w, got, pending, failed


def test_extract_ks_photos_merges_sibling_author():
    from douyin_dl.watcher import extract_ks_photos
    phs = extract_ks_photos(ks_feed_body(ks_photo()))
    assert len(phs) == 1
    assert phs[0]["author"]["name"] == "义沉沉"   # 平级 author 被合进来
    info = core.parse_kuaishou(phs[0])
    assert info.author == "义沉沉"


def test_ks_url_detection():
    assert BrowserWatcher._is_ks_url("https://www.kuaishou.com/rest/v/feed/hot")
    assert BrowserWatcher._is_ks_url("/rest/v/photo/collect")     # 相对路径
    assert BrowserWatcher._is_ks_url("https://www.gifshow.com/x")
    assert not BrowserWatcher._is_ks_url("https://www.douyin.com/aweme/v1/web/aweme/detail")


def test_ks_collect_action_values():
    from douyin_dl.watcher import ks_action_value, collect_ks_ids, ks_ids_from_url
    assert ks_action_value("/rest/v/photo/collect", {}, {"collect": 1}) == "1"
    assert ks_action_value("/rest/v/photo/collect", {}, {"collect": 2}) == "0"
    assert ks_action_value("/rest/v/photo/collect?collect=1", {}, {}) == "1"
    assert ks_action_value("/rest/v/photo/collect?collect=2", {}, {}) == "0"
    assert ks_action_value("/rest/v/photo/collect", {}, {}) == ""
    assert collect_ks_ids({"photoId": "3xb42hegyea3mzc"}) == ["3xb42hegyea3mzc"]
    assert ks_ids_from_url("https://www.kuaishou.com/short-video/3xb42hegyea3mzc") == [
        "3xb42hegyea3mzc"]
    assert ks_ids_from_url("https://www.kuaishou.com/f?photoId=3xb42hegyea3mzc") == [
        "3xb42hegyea3mzc"]


def test_ks_data_then_collect_downloads_without_refetch():
    """核心路径: feed 缓存 → 收藏动作 → 直接提交(不进 _pending_actions)。"""
    w, got, pending, failed = _new_watcher()
    body = ks_feed_body(ks_photo())
    w._on_ks_payload("data", "https://www.kuaishou.com/rest/v/feed/hot",
                     body, {}, json.dumps(body), "")
    assert w._get_cached("3xb42hegyea3mzc") is not None
    assert got == []                                    # 只缓存, 不触发下载
    req = {"photoId": "3xb42hegyea3mzc", "userId": "u1", "collect": 1}
    w._on_ks_payload("action", "https://www.kuaishou.com/rest/v/photo/collect",
                     {"result": 1}, req, '{"result":1}', json.dumps(req))
    assert got == [("3xb42hegyea3mzc", "快手收藏")]
    assert pending == []                                # 零延迟, 不重抓
    assert failed == []


def test_ks_uncollect_does_not_download():
    w, got, pending, failed = _new_watcher()
    w._on_ks_payload("data", "https://www.kuaishou.com/rest/v/feed/hot",
                     ks_feed_body(ks_photo()), {}, "{}", "")
    req = {"photoId": "3xb42hegyea3mzc", "collect": 2}
    w._on_ks_payload("action", "https://www.kuaishou.com/rest/v/photo/collect",
                     {"result": 1}, req, '{"result":1}', json.dumps(req))
    assert got == [] and pending == []


def test_ks_no_collect_field_ignored():
    w, got, pending, failed = _new_watcher()
    w._on_ks_payload("data", "https://www.kuaishou.com/rest/v/feed/hot",
                     ks_feed_body(ks_photo()), {}, "{}", "")
    req = {"photoId": "3xb42hegyea3mzc"}              # 状态同步, 无 collect
    w._on_ks_payload("action", "https://www.kuaishou.com/rest/v/photo/collect",
                     {"result": 1}, req, '{"result":1}', json.dumps(req))
    assert got == [] and pending == []


def test_ks_collect_cache_miss_goes_pending():
    """冷启动没缓存: 占位 + 交给无头兜底, 不静默丢掉。"""
    w, got, pending, failed = _new_watcher()
    req = {"photoId": "3xb42hegyea3mzc", "collect": 1}
    w._on_ks_payload("action", "https://www.kuaishou.com/rest/v/photo/collect",
                     {"result": 1}, req, '{"result":1}', json.dumps(req))
    assert got == []
    assert pending == ["3xb42hegyea3mzc"]
    assert w._pending_actions == ["3xb42hegyea3mzc"]


def test_ks_uncollect_cancels_pending():
    w, got, pending, failed = _new_watcher()
    w._pending_actions.append("3xb42hegyea3mzc")
    req = {"photoId": "3xb42hegyea3mzc", "collect": 2}
    w._on_ks_payload("action", "https://www.kuaishou.com/rest/v/photo/collect",
                     {"result": 1}, req, '{"result":1}', json.dumps(req))
    assert w._pending_actions == []


def test_ks_collect_list_only_builds_baseline():
    w, got, pending, failed = _new_watcher()
    body = ks_feed_body(ks_photo(), ks_photo("3xcv6y26jmteshe"))
    w._on_ks_payload("data", "https://www.kuaishou.com/rest/v/collect/list",
                     body, {}, json.dumps(body), "")
    assert w.baseline_done
    assert "3xb42hegyea3mzc" in w.known_favs
    assert got == []                                    # 绝不触发下载


def test_ks_collect_list_download_all_on_start():
    w, got, pending, failed = _new_watcher(download_all_on_start=True)
    body = ks_feed_body(ks_photo())
    w._on_ks_payload("data", "https://www.kuaishou.com/rest/v/collect/list",
                     body, {}, json.dumps(body), "")
    assert [a for a, s in got] == ["3xb42hegyea3mzc"]
    assert all(s == "快手收藏列表" for a, s in got)


def test_ks_auto_recommend_submits_feed():
    w, got, pending, failed = _new_watcher(auto_recommend=True)
    body = ks_feed_body(ks_photo(), ks_photo("3xcv6y26jmteshe"))
    w._on_ks_payload("data", "https://www.kuaishou.com/rest/v/feed/hot",
                     body, {}, json.dumps(body), "")
    assert len(got) == 2
    assert all(s == "快手推荐" for a, s in got)
    # 再来一次同样的数据 → 去重, 不重复提交
    w._on_ks_payload("data", "https://www.kuaishou.com/rest/v/feed/hot",
                     body, {}, json.dumps(body), "")
    assert len(got) == 2


def test_on_payload_routes_kuaishou_away_from_douyin():
    """快手流量绝不能被抖音分支误处理。"""
    w, got, pending, failed = _new_watcher()
    body = ks_feed_body(ks_photo())
    w._on_payload("data", "https://www.kuaishou.com/rest/v/feed/hot",
                  json.dumps(body), "", None)
    assert w._get_cached("3xb42hegyea3mzc") is not None
    assert got == []


def test_douyin_payload_still_goes_to_douyin_branch():
    """回归: 抖音收藏列表仍走原分支(只建基线, 不下)。"""
    w, got, pending, failed = _new_watcher()
    body = json.dumps({"aweme_list": [
        {"aweme_id": "7705427599274719717", "desc": "抖音",
         "video": {"bit_rate": [{"play_addr": {"url_list": ["https://v/a.mp4"]}}]}}
    ]})
    w._on_payload("data",
                  "https://www-hj.douyin.com/aweme/v1/web/aweme/listcollection/?count=1",
                  body, "", None)
    assert w.baseline_done
    assert got == []


# ---------------------------------------------------------------------------
# 5. 下载接线: parse_any / refetch_aweme / submit 缓存
# ---------------------------------------------------------------------------
def _load_main():
    spec = importlib.util.spec_from_file_location("dy_main_ks", _ROOT / "douyin_dl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_downloader_ks_full_path(tmp_path, monkeypatch):
    """快手完整下载链路: process() 落盘 + 文件名/meta/state/事件 全部走通。

    之前只测到 state.has()==True 的跳过分支, 真正的下载分支没验证过。
    这里把网络层打桩, 用真实 mp4 魔数落盘, 检查快手 photoId 能贯穿到
    文件名、_meta 和 state.mark(字母数字混排 id 曾被 _safe_id 拦成空串)。
    """
    mod = _load_main()
    ph = ks_photo()
    wrapped = {"aweme_id": ph["id"], "_platform": PLATFORM_KUAISHOU, "_ks": ph}

    monkeypatch.setattr(mod, "get_out_dir", lambda: tmp_path)
    monkeypatch.setattr(mod, "load_settings", lambda _p: {"quality": "max"})
    monkeypatch.setattr(mod, "probe_size", lambda session, url: 23089393)
    monkeypatch.setattr(mod, "verify_video", lambda dest: True)

    def fake_download(session, url, dest, log=None, on_progress=None,
                      **kwargs):
        # 真 mp4 魔数, 让后续校验逻辑有意义
        dest.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4096)
        return True

    # v2.2: 单流下载改走 download_smart(大文件自动分段并发), 小文件最终还是
    # 落到 download。两个都打桩。
    monkeypatch.setattr(mod, "download", fake_download)
    monkeypatch.setattr(mod, "download_smart", fake_download)

    class FakeState:
        def __init__(self):
            self.marked = []

        def has(self, _): return False

        def mark(self, aid, dest, res):
            self.marked.append((aid, dest, res))

    dl = mod.Downloader.__new__(mod.Downloader)
    dl.session = None
    dl.state = FakeState()
    dl.bus = None
    dl.log = lambda *a, **k: None
    dl.count_ok = dl.count_skip = dl.count_fail = 0
    dl._rec_prefetch = {}
    events = []
    dl._ev = lambda name, *a: events.append((name, a))

    dl.process(wrapped, "快手收藏")

    assert dl.count_ok == 1 and dl.count_fail == 0
    assert dl.state.marked and dl.state.marked[0][0] == "3xb42hegyea3mzc"
    dest = dl.state.marked[0][1]
    assert dest.suffix == ".mp4" and dest.name.endswith("_3xb42hegyea3mzc.mp4")
    assert dest.exists()
    # begin 事件的第一个参数必须也是快手 id(面板据此更新进度)
    begins = [a for n, a in events if n == "begin"]
    assert begins and begins[0][0] == "3xb42hegyea3mzc"
    assert begins[0][1]["quality"] == "960x720"
    assert [n for n, a in events if n == "finish"] == ["finish"]
    # _meta 也以 photoId 命名, 且 quality/候选档位写对
    meta = json.loads((tmp_path / "_meta" / "3xb42hegyea3mzc.json").read_text(
        encoding="utf-8"))
    assert meta["aweme_id"] == "3xb42hegyea3mzc"
    assert meta["author"] == "" or isinstance(meta["author"], str)
    assert len(meta["all_candidates"]) == 4
    assert meta["quality"] == "960x720"


def test_downloader_submit_caches_ks_for_retry(monkeypatch):
    mod = _load_main()
    mod._KS_LAST.clear()
    dl = mod.Downloader.__new__(mod.Downloader)
    dl.q = __import__("queue").Queue()
    dl.bus = None
    ph = ks_photo()
    wrapped = {"aweme_id": ph["id"], "_platform": PLATFORM_KUAISHOU, "_ks": ph}
    dl.submit(wrapped, "快手收藏")
    assert mod._KS_LAST.get(ph["id"]) is wrapped
    # 重试快手: 零网络, 直接拿回缓存对象
    mod._hd_q = __import__("queue").Queue()   # 确保不会被误用
    called = []
    monkeypatch.setattr(mod, "_hd_fetch", lambda aid: called.append(aid))
    assert mod.refetch_aweme(ph["id"], None) is wrapped
    assert called == []


def test_refetch_ks_cache_miss_returns_none_without_douyin_fetch(monkeypatch):
    mod = _load_main()
    mod._KS_LAST.clear()
    called = []
    monkeypatch.setattr(mod, "_hd_fetch", lambda aid: called.append(aid) or {"x": 1})
    assert mod.refetch_aweme("3xb42hegyea3mzc", None) is None
    assert called == []                                # 不能拿 photoId 开抖音详情页


def test_hook_smoke_real_browser():
    """真浏览器注入 HOOK_JS: 用子进程跑, 避开 pytest-asyncio 的事件循环冲突。"""
    import subprocess
    r = subprocess.run(
        [sys.executable, str(_ROOT / "tests" / "hook_smoke.py")],
        cwd=str(_ROOT), capture_output=True, timeout=90,
        # Windows 下 subprocess 默认拿 locale(GBK) 解码子进程输出, 而子进程
        # 按 PYTHONIOENCODING 写的是 utf-8 —— 不显式指定就会 UnicodeDecodeError。
        encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 2:
        import pytest
        pytest.skip("系统 Chrome 不可用, 跳过钩子注入冒烟")
    assert r.returncode == 0, out[-1500:]


def test_downloader_process_uses_parse_any(monkeypatch):
    """快手包装对象进 process 时, parse_any 生效且画质/文件名逻辑能跑。"""
    mod = _load_main()
    ph = ks_photo()
    wrapped = {"aweme_id": ph["id"], "_platform": PLATFORM_KUAISHOU, "_ks": ph}

    class FakeState:
        def has(self, _): return True
        def mark(self, *a, **k): pass

    dl = mod.Downloader.__new__(mod.Downloader)
    dl.session = None
    dl.state = FakeState()
    dl.bus = None
    dl._ev = lambda *a, **k: None
    dl.log = lambda *a, **k: None
    dl.count_skip = 0
    dl.count_fail = 0
    dl._rec_prefetch = {}
    # 走 state.has()==True 的 skip 分支即可证明 parse_any 没抛异常、
    # 且 info.aweme_id 被正确解析成快手 id(否则不会走到 skip)
    dl.process(wrapped, "快手收藏")
    assert dl.count_skip == 1
