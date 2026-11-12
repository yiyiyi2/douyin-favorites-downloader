"""畸形 / 未来形态输入轰炸测试(由 v1.6 的临时 fuzz 探针固化而来)。

抖音的 JSON 结构、用户的 .state.json/.settings.json 会被接口改版、手工编辑、
半截断电写坏成任意形状。这里断言："无论喂什么, 解析/面板都不能崩"。
"""
import json
from pathlib import Path

import pytest

import douyin_dl.ui as ui
from douyin_dl.core import build_filename, parse_aweme, safe_name


# ---------------------------------------------------------------------------
# 1. core.parse_aweme: "将来某字段换个类型" 也不能崩
# ---------------------------------------------------------------------------
PARSE_CASES = {
    "正常": {"aweme_id": "123", "desc": "标题", "create_time": 1700000000,
             "video": {"duration": 15000, "width": 1080, "height": 1920,
                       "cover": {"url_list": ["https://x/a.jpg"]},
                       "play_addr": {"url_list": ["https://x/a.mp4"], "width": 1080,
                                     "height": 1920, "data_size": 123456}},
             "author": {"nickname": "作者", "uid": "u1"},
             "music": {"title": "曲", "author": "人"}},
    "空对象": {},
    "顶层 null": {"aweme_id": None, "desc": None, "create_time": None, "video": None,
                  "author": None, "music": None, "images": None},
    "video 变列表": {"aweme_id": "1", "video": [1, 2, 3]},
    "video 变字符串": {"aweme_id": "1", "video": "https://x/a.mp4"},
    "author 变字符串": {"aweme_id": "1", "author": "作者名"},
    "music 变列表": {"aweme_id": "1", "music": [1, 2]},
    "desc 变字典": {"aweme_id": "1", "desc": {"text": "标题"}},
    "desc 变数字": {"aweme_id": "1", "desc": 12345},
    "create_time 带毫秒": {"aweme_id": "1", "create_time": 1700000000000},
    "create_time 变日期字符串": {"aweme_id": "1", "create_time": "2024-01-01"},
    "create_time 负数": {"aweme_id": "1", "create_time": -1},
    "duration 变字符串": {"aweme_id": "1", "video": {"duration": "15s"}},
    "duration 变字典": {"aweme_id": "1", "video": {"duration": {"ms": 15000}}},
    "cover 变字符串": {"aweme_id": "1", "video": {"cover": "https://x/a.jpg"}},
    "cover.url_list 变字符串": {"aweme_id": "1", "video": {"cover": {"url_list": "https://x/a.jpg"}}},
    "images 变字典": {"aweme_id": "1", "images": {"1": {"url_list": ["https://x/1.jpg"]}}},
    "images 变字符串": {"aweme_id": "1", "images": "abc"},
    "images 元素是字符串": {"aweme_id": "1", "images": ["https://x/1.jpg"]},
    "images 元素 null": {"aweme_id": "1", "images": [None]},
    "bit_rate 变字典": {"aweme_id": "1", "video": {"bit_rate": {"a": {"play_addr": {}}}}},
    "bit_rate 元素 null": {"aweme_id": "1", "video": {"bit_rate": [None, 1, "x"]}},
    "width/height 变字符串": {"aweme_id": "1", "video": {
        "play_addr": {"url_list": ["https://x/a.mp4"], "width": "1080", "height": "1920"}}},
    "width 变 1080p": {"aweme_id": "1", "video": {
        "play_addr": {"url_list": ["https://x/a.mp4"], "width": "1080p"}}},
    "data_size 变字典": {"aweme_id": "1", "video": {
        "play_addr": {"url_list": ["https://x/a.mp4"], "data_size": {"bytes": 100}}}},
    "bit_rate 变 1.5M": {"aweme_id": "1", "video": {"bit_rate": [
        {"bit_rate": "1.5M", "play_addr": {"url_list": ["https://x/a.mp4"]}}]}},
    "play_addr.url_list 变字符串": {"aweme_id": "1", "video": {
        "play_addr": {"url_list": "https://x/a.mp4"}}},
    "play_addr.url_list 含 null": {"aweme_id": "1", "video": {
        "play_addr": {"url_list": [None, 123, "https://x/a.mp4"]}}},
    "nickname 变数字": {"aweme_id": "1", "author": {"nickname": 12345}},
    "uid 变字典": {"aweme_id": "1", "author": {"uid": {"v": "u1"}}},
    "嵌套极深字段类型全换": {"aweme_id": "1", "video": {
        "cover": [1], "origin_cover": 3,
        "bit_rate": [{"play_addr": "not-dict", "download_addr": None}]}},
    "整个 aweme 是列表": [1, 2, 3],
    "整个 aweme 是字符串": "oops",
    "整个 aweme 是 None": None,
}


@pytest.mark.parametrize("name", list(PARSE_CASES))
def test_parse_aweme_never_crashes(name):
    info = parse_aweme(PARSE_CASES[name])
    # 解析结果喂给文件名构造也不能崩(标题可能是 dict/list/数字)
    fn = build_filename(info)
    assert isinstance(fn, str)
    assert safe_name(fn) == fn or isinstance(safe_name(fn), str)


# ---------------------------------------------------------------------------
# 2. 面板: .state.json / .settings.json 被写坏成任意形状也不能崩
# ---------------------------------------------------------------------------
STATE_CASES = {
    "正常": {"1111111111111111111": {"file": "downloads/a.mp4", "quality": "1080x1920", "time": 1700000000}},
    "损坏 JSON": "{{{ not json",
    "顶层是列表": [1, 2, 3],
    "顶层是字符串": "hello",
    "顶层是 null": None,
    "row 是字符串": {"1111111111111111111": "oops"},
    "row 是 null": {"1111111111111111111": None},
    "row 是数字": {"1111111111111111111": 123},
    "row 是列表": {"1111111111111111111": [1, 2]},
    "file 是绝对路径": {"1111111111111111111": {"file": "C:/tmp/a.mp4", "time": 1700000000}},
    "file 是 null": {"1111111111111111111": {"file": None, "time": 1700000000}},
    "file 是数字": {"1111111111111111111": {"file": 12345, "time": 1700000000}},
    "file 是字典": {"1111111111111111111": {"file": {"a": 1}, "time": 1700000000}},
    "file 路径穿越": {"1111111111111111111": {"file": "../../etc/passwd", "time": 1700000000}},
    "time 是字符串": {"1111111111111111111": {"file": "a.mp4", "time": "2024-01-01"}},
    "time 是 null": {"1111111111111111111": {"file": "a.mp4", "time": None}},
    "time 是列表": {"1111111111111111111": {"file": "a.mp4", "time": [1]}},
    "size 是字符串": {"1111111111111111111": {"file": "a.mp4", "time": 1, "size": "big"}},
    "quality 是 null": {"1111111111111111111": {"file": "a.mp4", "time": 1, "quality": None}},
    "键不是 id": {"../../etc/passwd": {"file": "a.mp4", "time": 1}},
    "混合脏数据": {"1111111111111111111": {"file": "a.mp4", "time": 1},
                   "2222222222222222222": None, "3333333333333333333": 5,
                   "4444444444444444444": {"file": None, "time": "x"}},
}

SETTINGS_CASES = {
    "正常": {"quality": "max", "auto_rec": False, "auto_gap": 3.0},
    "损坏 JSON": "{{{",
    "顶层列表": [1, 2],
    "顶层 null": None,
    "quality 非法": {"quality": "99999p"},
    "quality 数字": {"quality": 1080},
    "quality null": {"quality": None},
    "auto_gap 字符串": {"auto_gap": "abc"},
    "auto_gap null": {"auto_gap": None},
    "auto_gap 负数": {"auto_gap": -50},
    "auto_gap 巨大": {"auto_gap": 1e300},
    "auto_rec 字符串": {"auto_rec": "false"},
    "auto_adv 数字": {"auto_adv": 1},
    "out_dir 是数字": {"out_dir": 123},
    "out_dir 不存在盘": {"out_dir": "Z:/nope/x"},
    "全字段类型错": {"quality": [], "by_author": "yes", "auto_reopen": "no",
                     "auto_rec": {}, "auto_gap": [], "auto_adv": [], "out_dir": 9},
}


def _write(path: Path, payload):
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("name", list(STATE_CASES))
def test_state_malformed_never_crashes_panel(name, tmp_path):
    sf = tmp_path / ".state.json"
    _write(sf, STATE_CASES[name])
    (tmp_path / ".settings.json").write_text("{}", encoding="utf-8")
    bus = ui.EventBus(tmp_path, tmp_path / "downloads", sf, tmp_path / "log.txt")
    snap = bus.snapshot()
    assert isinstance(json.dumps(snap, ensure_ascii=False), str)
    assert isinstance(bus._recent(), list)


@pytest.mark.parametrize("name", list(SETTINGS_CASES))
def test_settings_malformed_never_crashes_panel(name, tmp_path):
    (tmp_path / ".state.json").write_text("{}", encoding="utf-8")
    cf = tmp_path / ".settings.json"
    _write(cf, SETTINGS_CASES[name])
    bus = ui.EventBus(tmp_path, tmp_path / "downloads", tmp_path / ".state.json", tmp_path / "log.txt")
    snap = bus.snapshot()
    assert isinstance(json.dumps(snap, ensure_ascii=False), str)
    cur = ui.load_settings(cf)
    assert isinstance(cur, dict)
    assert isinstance(ui._clamp_gap(cur.get("auto_gap")), float)
    assert isinstance(ui.filter_candidates([], cur.get("quality")), list)
