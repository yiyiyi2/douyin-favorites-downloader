# -*- coding: utf-8 -*-
"""watcher 判定逻辑单测: 基线 / 新增收藏 / 取消收藏 / 动作补全。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from douyin_dl.watcher import BrowserWatcher, extract_awemes, collect_ids, is_uncollect


def make_aweme(aid, desc="视频", res=(1080, 1920), br=3200000):
    return {
        "aweme_id": aid,
        "desc": desc,
        "create_time": 1730000000,
        "author": {"nickname": "作者A", "uid": "1"},
        "video": {
            "duration": 10000,
            "bit_rate": [{
                "gear_name": "g",
                "bit_rate": br,
                "play_addr": {"width": res[0], "height": res[1],
                              "url_list": [f"https://v/{aid}.mp4"]},
            }],
        },
    }


def listcollection_body(ids):
    return json.dumps({"aweme_list": [make_aweme(i) for i in ids],
                       "has_more": 0, "status_code": 0})


import time as _time

# 模拟「用户刚刚点了收藏」的钩子元数据: favIds 必须是点击时抓到的那个视频
def clicked(*aids):
    """模拟点击收藏: 可以一次传入多个 id(连续快速点了几个)。"""
    now = int(_time.time() * 1000)
    ids = [str(a) for a in aids]
    return {"favAt": now, "href": "", "favIds": ids,
            "favTs": {i: now for i in ids}}


def new_watcher(download_all=False):
    got = []
    w = BrowserWatcher(profile_dir=Path("."), on_aweme=lambda a, s: got.append((a["aweme_id"], s)))
    w.download_all_on_start = download_all
    w.log = lambda *a, **k: None
    return w, got


def test_extract():
    body = json.loads(listcollection_body(["111", "222"]))
    aws = extract_awemes(body)
    assert len(aws) == 2, aws
    assert [a["aweme_id"] for a in aws] == ["111", "222"]
    print("✔ extract_awemes 能挖出列表里的视频")


def test_baseline_no_download():
    w, got = new_watcher(download_all=False)
    w._on_payload("data", "https://www.douyin.com/aweme/v1/web/aweme/listcollection/?cursor=0",
                  listcollection_body(["1", "2", "3"]), "")
    assert got == [], f"基线阶段不应下载, 实际 {got}"
    assert w.known_favs == {"1", "2", "3"}
    print("✔ 首次收藏列表只做基线, 不下载")


def test_list_refresh_never_downloads():
    """收藏列表第一页自己轮换(旧视频冒到头部)也不算新收藏。"""
    w, got = new_watcher()
    w._on_payload("data", "x/aweme/listcollection?cursor=0",
                  listcollection_body(["1", "2"]), "")
    w._on_payload("data", "x/aweme/listcollection?cursor=0",
                  listcollection_body(["3", "1", "2"]), "")
    assert got == [], f"列表刷新不应触发下载, 实际 {got}"
    print("✔ 收藏列表刷新不再误下")


def test_download_all_backfills():
    w, got = new_watcher(download_all=True)
    w._on_payload("data", "https://www.douyin.com/aweme/v1/web/aweme/listcollection/?cursor=0",
                  listcollection_body(["7", "8"]), "")
    assert sorted(g[0] for g in got) == ["7", "8"], got
    print("✔ --download-all 会回补已有收藏")


def test_action_always_refetches():
    """点收藏后必须重新抓详情: 缓存里的播放地址签名会过期, 复用就 403。"""
    w, got = new_watcher()
    w._on_payload("data", "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=7300000000000000999",
                  json.dumps({"aweme_detail": make_aweme("7300000000000000999")}), "")
    assert "7300000000000000999" in w.cache
    w._on_payload("action", "https://www.douyin.com/aweme/v1/web/commit/item/",
                  json.dumps({"status_code": 0}),
                  json.dumps({"aweme_id": "7300000000000000999", "action": 1}),
                  clicked("7300000000000000999"))
    assert got == [], got
    assert w._pending_actions == ["7300000000000000999"], w._pending_actions
    print("✔ 点收藏后不走缓存, 而是重新抓详情")


def test_action_unknown_goes_pending():
    w, got = new_watcher()
    w._on_payload("action", "https://www.douyin.com/aweme/v1/web/aweme/collect/",
                  json.dumps({"status_code": 0}), json.dumps({"aweme_id": "123456789012345678", "action": 1}),
                  clicked("123456789012345678"))
    assert got == [], got
    assert w._pending_actions == ["123456789012345678"], w._pending_actions
    print("✔ 缓存里没有的收藏会进待补全队列")


def test_uncollect_ignored():
    w, got = new_watcher()
    w._on_payload("data", "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=7300000000000000555",
                  json.dumps({"aweme_detail": make_aweme("7300000000000000555")}), "")
    # action=0 才是取消收藏 (实测: action=1 收藏 / action=0 取消)
    w._on_payload("action", "https://www.douyin.com/aweme/v1/web/aweme/collect/",
                  json.dumps({"status_code": 0}), json.dumps({"aweme_id": "7300000000000000555", "action": 0}),
                  clicked("7300000000000000999"))
    assert got == [], f"取消收藏不应下载, 实际 {got}"
    print("✔ 取消收藏不会被误下载")
    assert is_uncollect("", {}, {"action": 0}) is True   # 0 = 取消收藏
    assert is_uncollect("", {}, {"action": 1}) is False  # 1 = 收藏
    assert is_uncollect("", {}, {}) is False            # 没有 action 的是页面同步请求, 不算取消


def test_collect_ids():
    ids = collect_ids({"aweme_id": "7300000000000000001"})
    assert ids == ["7300000000000000001"]
    ids2 = collect_ids({"data": {"aweme_list": [{"aweme_id": "7300000000000000002"}]}})
    assert ids2 == ["7300000000000000002"]
    # 太短的数字(不是 aweme_id)要过滤掉
    assert collect_ids({"item_id": "123"}) == []
    print("✔ collect_ids 能抓 id 且过滤噪音")


def test_cache_limit():
    w, _ = new_watcher()
    w.cache_limit = 5
    for i in range(12):
        w._on_payload("data", "x/aweme/detail", json.dumps({"aweme_detail": make_aweme(str(100 + i))}), "")
    assert len(w.cache) <= 5, len(w.cache)
    print("✔ 缓存有上限, 不会无限增长")


def test_pagination_never_downloads():
    """翻页(cursor!=0)加载出来的老收藏, 绝不能当新收藏下载。"""
    w, got = new_watcher()
    w._on_payload("data", "https://www.douyin.com/aweme/v1/web/aweme/listcollection/?cursor=0",
                  listcollection_body(["1", "2"]), "")
    assert got == []
    w._on_payload("data", "https://www.douyin.com/aweme/v1/web/aweme/listcollection/?cursor=20",
                  listcollection_body(["9", "8", "7", "6"]), "")
    assert got == [], f"翻页不应触发下载, 实际 {got}"
    print("✔ 翻页加载的老收藏不会被下载")


def test_action_takes_request_id_only():
    """收藏动作的响应体里带着整页列表时, 只认请求体里那一个 id。"""
    w, got = new_watcher()
    body = json.dumps({"aweme_list": [{"aweme_id": "7300000000000000011"},
                                      {"aweme_id": "7300000000000000012"}]})
    w._on_payload("action", "x/aweme/collect", body,
                  json.dumps({"aweme_id": "7300000000000000099", "action": 1}), clicked("7300000000000000099"))
    assert w._pending_actions == ["7300000000000000099"], w._pending_actions
    print("✔ 收藏动作只取请求体里的那一个 id")


def test_page_sync_request_ignored():
    """页面自己发的状态同步请求不带 action, 必须忽略(它们不是人点的)。"""
    w, got = new_watcher()
    w._on_payload("data", "x/aweme/detail", json.dumps({"aweme_detail": make_aweme("7300000000000000888")}), "")
    w._on_payload("action", "x/aweme/commit/item/", json.dumps({"status_code": 0}),
                  json.dumps({"aweme_id": "7300000000000000888", "item_type": 0, "type": 0}), {})
    assert got == [], f"状态同步请求不该下载, 实际 {got}"
    assert w._pending_actions == []
    print("✔ 页面的状态同步请求被过滤")


def test_action_one_downloads_without_click():
    """实测: action=1 一定是人点的收藏, 即使钩子没抓到点击也要下载。"""
    w, got = new_watcher()
    w._on_payload("data", "x/aweme/detail", json.dumps({"aweme_detail": make_aweme("7300000000000000666")}), "")
    w._on_payload("action", "x/aweme/collect", json.dumps({"status_code": 0}),
                  json.dumps({"aweme_id": "7300000000000000666", "action": 1}), {})
    assert got == [], got
    assert w._pending_actions == ["7300000000000000666"], w._pending_actions
    print("✔ action=1 即使没有点击记录也会去抓详情下载")


def test_fav_by_current_video():
    """即使没抓到点击时间戳, 收藏的正是当前播放的视频, 也算真收藏。"""
    w, got = new_watcher()
    w._on_payload("data", "x/aweme/detail", json.dumps({"aweme_detail": make_aweme("7300000000000000777")}), "")
    meta = {"favAt": 0, "href": "https://www.douyin.com/video/7300000000000000777", "favIds": []}
    w._on_payload("action", "x/aweme/collect", json.dumps({"status_code": 0}),
                  json.dumps({"aweme_id": "7300000000000000777", "action": 1}), meta)
    assert got == [], got
    assert w._pending_actions == ["7300000000000000777"], w._pending_actions
    print("✔ 收藏当前播放视频会去抓详情下载")


def test_two_rapid_clicks_both_download():
    """连续快速收藏两个视频, 两个都要下, 不能只下最后一个。"""
    w, got = new_watcher()
    a, b = "7300000000000000aa"[:0] or "7300000000000000111", "7300000000000000222"
    for aid in (a, b):
        w._on_payload("data", f"x/aweme/detail/?aweme_id={aid}",
                      json.dumps({"aweme_detail": make_aweme(aid)}), "")
    meta = clicked(a, b)          # 两次点击都记在同一个点击记录里
    w._on_payload("action", "x/aweme/collect", json.dumps({"status_code": 0}),
                  json.dumps({"aweme_id": a, "action": 1}), meta)
    w._on_payload("action", "x/aweme/collect", json.dumps({"status_code": 0}),
                  json.dumps({"aweme_id": b, "action": 1}), meta)
    assert sorted(w._pending_actions) == sorted([a, b]), w._pending_actions
    print("✔ 连续两次收藏都会进队抓详情")


if __name__ == "__main__":
    for fn in [
        test_extract, test_baseline_no_download, test_list_refresh_never_downloads,
        test_download_all_backfills, test_action_always_refetches, test_action_unknown_goes_pending,
        test_uncollect_ignored, test_collect_ids, test_cache_limit,
        test_pagination_never_downloads, test_action_takes_request_id_only,
        test_page_sync_request_ignored, test_action_one_downloads_without_click,
        test_fav_by_current_video,
        test_two_rapid_clicks_both_download,
    ]:
        fn()
    print("\n=== watcher 逻辑测试全部通过 ===")
