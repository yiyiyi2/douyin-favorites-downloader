"""v1.4: 自动下载推荐页视频(auto_rec)的行为测试。"""
import json
import tempfile
from pathlib import Path

from douyin_dl.watcher import BrowserWatcher


def _mk_watcher():
    tmp = Path(tempfile.mkdtemp(prefix="dy_auto_"))
    w = BrowserWatcher(profile_dir=tmp, log=lambda *a: None)
    submits: list[tuple[str, str]] = []
    w.on_aweme = lambda aw, src: submits.append((aw["aweme_id"], src))
    return w, submits


def _payload(aids):
    return json.dumps({"aweme_list": [
        {"aweme_id": a, "desc": f"v{a}", "video": {"bit_rate": []}} for a in aids
    ]})


def test_off_no_trigger():
    w, submits = _mk_watcher()
    w._on_payload("data", "https://www-hj.douyin.com/aweme/v1/web/tab/feed/?count=10",
                  _payload(["111", "222"]), None)
    assert submits == []


def test_on_feed_queues_all():
    w, submits = _mk_watcher()
    w.auto_recommend = True
    w._on_payload("data", "https://www-hj.douyin.com/aweme/v1/web/tab/feed/?count=10",
                  _payload(["111", "222", "333"]), None)
    assert sorted(submits) == [("111", "推荐页"), ("222", "推荐页"), ("333", "推荐页")]


def test_session_dedupe():
    w, submits = _mk_watcher()
    w.auto_recommend = True
    p = _payload(["111", "222", "333"])
    w._on_payload("data", "https://www-hj.douyin.com/aweme/v1/web/tab/feed/?count=10", p, None)
    w._on_payload("data", "https://www-hj.douyin.com/aweme/v1/web/tab/feed/?count=10", p, None)
    assert len(submits) == 3


def test_related_endpoint_also_triggers():
    w, submits = _mk_watcher()
    w.auto_recommend = True
    w._on_payload("data", "https://www-hj.douyin.com/aweme/v1/web/tab/feed/?count=10",
                  _payload(["111"]), None)
    w._on_payload("data", "https://www-hj.douyin.com/aweme/v1/web/aweme/related/?aweme_id=111",
                  _payload(["444", "555"]), None)
    assert ("444", "推荐页") in submits and ("555", "推荐页") in submits


def test_known_favs_dedupe():
    w, submits = _mk_watcher()
    w.auto_recommend = True
    w.known_favs.add("444")
    w._on_payload("data", "https://www-hj.douyin.com/aweme/v1/web/aweme/related/?aweme_id=x",
                  _payload(["444", "666"]), None)
    ids = [s[0] for s in submits]
    # 444 已在收藏基线里, 不该再从推荐页重复提交; 666 是新视频, 正常进队
    assert "444" not in ids and "666" in ids


def test_listcollection_not_affected():
    w, submits = _mk_watcher()
    w.auto_recommend = True
    w._on_payload("data",
                  "https://www-hj.douyin.com/aweme/v1/web/aweme/listcollection/?cursor=0&count=1",
                  _payload(["777"]), None)
    assert submits == []


def test_switch_off_stops_immediately():
    w, submits = _mk_watcher()
    w.auto_recommend = True
    w._on_payload("data", "https://www-hj.douyin.com/aweme/v1/web/tab/feed/?count=10",
                  _payload(["111"]), None)
    assert len(submits) == 1
    w.auto_recommend = False
    w._on_payload("data", "https://www-hj.douyin.com/aweme/v1/web/tab/feed/?count=10",
                  _payload(["888"]), None)
    assert len(submits) == 1
