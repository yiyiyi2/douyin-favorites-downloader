"""自动翻页开关 / 间隔 / 背压 的离线测试(不起浏览器, 不联网)。"""
import pathlib
import tempfile
import time

import pytest

from douyin_dl.ui import _clamp_gap
from douyin_dl.watcher import BrowserWatcher


@pytest.fixture()
def watcher():
    w = BrowserWatcher(profile_dir=pathlib.Path(tempfile.mkdtemp()), on_aweme=lambda a, s: None)
    w.logs = []
    w.log = lambda m="": w.logs.append(m)
    w.pressed = []
    w.auto_recommend = True
    w.queue_probe = lambda: 0
    return w


class _K:
    def __init__(self, sink):
        self.sink = sink

    def press(self, k):
        self.sink.append(k)


class _Pg:
    def __init__(self, sink, url="https://www.douyin.com/?recommend=1"):
        self.url = url
        self.keyboard = _K(sink)

    def is_closed(self):
        return False


class _Ctx:
    def __init__(self, pages):
        self.pages = pages


def test_auto_advance_off_does_not_scroll(watcher):
    watcher.auto_advance = False
    watcher._auto_advance_feed(_Ctx([_Pg(watcher.pressed)]))
    assert watcher.pressed == []


def test_auto_advance_on_scrolls(watcher):
    watcher.auto_advance = True
    watcher._auto_advance_feed(_Ctx([_Pg(watcher.pressed)]))
    assert watcher.pressed == ["ArrowDown"]


def test_auto_recommend_off_does_not_scroll(watcher):
    watcher.auto_advance = True
    watcher.auto_recommend = False
    watcher._auto_advance_feed(_Ctx([_Pg(watcher.pressed)]))
    assert watcher.pressed == []


def test_backpressure_pauses_and_resumes(watcher):
    watcher.auto_advance = True
    watcher.auto_queue_high = 12

    watcher.queue_probe = lambda: 20
    watcher._auto_advance_feed(_Ctx([_Pg(watcher.pressed)]))
    assert watcher.pressed == []
    assert any("暂停翻页" in x for x in watcher.logs)

    watcher.queue_probe = lambda: 3
    watcher._auto_advance_feed(_Ctx([_Pg(watcher.pressed)]))
    assert watcher.pressed == ["ArrowDown"]
    assert any("继续翻页" in x for x in watcher.logs)


@pytest.mark.parametrize("url", [
    "https://www.douyin.com/search/abc",
    "https://www.douyin.com/video/7705352898574905203",
    "https://www.douyin.com/user/MS4wLjABAAAA",
    "https://www.douyin.com/follow",
])
def test_non_recommend_pages_are_not_scrolled(watcher, url):
    watcher.auto_advance = True
    watcher._auto_advance_feed(_Ctx([_Pg(watcher.pressed, url)]))
    assert watcher.pressed == []


def test_only_recommend_page_in_multi_page(watcher):
    watcher.auto_advance = True
    watcher._auto_advance_feed(_Ctx([
        _Pg(watcher.pressed, "https://www.douyin.com/search/x"),
        _Pg(watcher.pressed),
    ]))
    assert watcher.pressed == ["ArrowDown"]


def test_one_scroll_per_round(watcher):
    watcher.auto_advance = True
    watcher._auto_advance_feed(_Ctx([_Pg(watcher.pressed), _Pg(watcher.pressed), _Pg(watcher.pressed)]))
    assert watcher.pressed == ["ArrowDown"]


def test_gap_timing(watcher):
    watcher.auto_advance_gap = 3.0
    watcher._last_advance = time.monotonic()
    assert (time.monotonic() - watcher._last_advance) < 3.0
    watcher._last_advance = time.monotonic() - 3.1
    assert (time.monotonic() - watcher._last_advance) >= 3.0


@pytest.mark.parametrize("raw,expect", [
    (0, 1.0), (1, 1.0), (3, 3.0), (60, 60.0), (999, 60.0),
    ("abc", 3.0), (None, 3.0), (float("nan"), 3.0), (-5, 1.0),
])
def test_clamp_gap(raw, expect):
    assert _clamp_gap(raw) == expect
