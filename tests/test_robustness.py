"""v1.6 鲁棒性回归测试。

针对本轮修的每个"外部数据一变就崩/就错"的点, 用畸形/未来形态的输入断言行为,
而不是只靠旧测试"没破坏"。全部离线, 不起浏览器、不联网。
"""
import importlib.util
import json
import pathlib
import tempfile
import time
from pathlib import Path

import pytest

from douyin_dl.ui import (
    EventBus, _as_bool, _as_dict, _as_number, _clamp_gap, _rel_path,
)
from douyin_dl.watcher import (
    EXIT_BROWSER_FAIL, EXIT_INTENTIONAL, EXIT_LOGIN_TIMEOUT, EXIT_OK,
    BrowserWatcher, _cap_set, _norm_id, _action_norm, action_value,
    collect_ids, extract_awemes, ids_from_url,
)

_ROOT = Path(__file__).resolve().parent.parent


def _load_main():
    """主程序是顶层脚本 douyin_dl.py(与包 douyin_dl/ 同名), 用 importlib 加载。"""
    spec = importlib.util.spec_from_file_location("dy_main_robust", _ROOT / "douyin_dl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. watcher: id / action 类型归一化(以后接口字段形态变了不该整条链失效)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expect", [
    ("7705352898574905203", "7705352898574905203"),   # 现在 19 位
    (7705352898574905203, "7705352898574905203"),       # int
    ("123", "123"),                                     # 将来变短也不挡
    ("  abc-xyz_01  ", "abc-xyz_01"),                  # 带前缀/哈希
    (None, ""), ("", ""), (True, ""),                   # 噪音
    (float("nan"), ""), (float("inf"), ""),
    ({"x": 1}, ""), (["a"], ""),
    ("has space", ""),                                  # 含空格的垃圾
    ("x" * 65, ""),                                     # 超长垃圾
])
def test_norm_id(raw, expect):
    assert _norm_id(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    (1, "1"), ("1", "1"), (True, "1"),
    ("collect", "1"), ("favorite", "1"), ("like", "1"), ("yes", "1"),
    (0, "0"), ("0", "0"), (False, "0"),
    ("cancel", "0"), ("uncollect", "0"), ("remove", "0"),
    (None, ""), ("maybe", ""), ({"a": 1}, ""), ([], ""),
])
def test_action_norm(raw, expect):
    assert _action_norm(raw) == expect


def test_action_value_reads_request_body_when_url_has_none():
    """收藏动作参数更常在请求体, url 没有时得翻 req 再翻 body。"""
    assert action_value("https://www-hj.douyin.com/aweme/v1/web/collect/",
                        body={}, req='{"action": "collect", "aweme_id": "7705352898574905203"}') == "1"
    # 布尔形态(以前 str(True)="True" 不匹配)
    assert action_value("https://x/collect/", body={"action": True}) == "1"
    # url 里明确是取消, 优先级最高
    assert action_value("https://x/collect/?action=0", body={"action": 1}) == "0"


def test_collect_ids_noise_gate_but_keeps_hash_ids():
    """纯数字短值是枚举噪音(aweme_type=0); 但将来换成带前缀 id 要放行。"""
    body = {"aweme_type": 0, "type": 0, "item_id": "123",
            "awemeId": "obj-abc-9f8e7d", "aweme_ids": ["7705352898574905203", "7705352898574905204"]}
    ids = collect_ids(body)
    assert "obj-abc-9f8e7d" in ids          # 非数字 id 放行(新形态)
    assert "7705352898574905203" in ids
    assert "7705352898574905204" in ids
    assert "0" not in ids                    # 枚举噪音没混进来
    assert "123" not in ids                  # 短数字 id 没混进来


def test_extract_awemes_handles_aliases_and_json_string():
    """aweme 被包一层 / video 改名 / 整段是 JSON 字符串, 都得挖出来。"""
    inner = {"awemeId": "7705352898574905203", "aweme_detail": {"play_addr": {"url_list": ["x"]}}}
    # 包两层 + 整段 aweme 是 JSON 字符串
    payload = {"data": {"extra": 1, "list": [json.dumps(inner, ensure_ascii=False)]}}
    found = extract_awemes(payload)
    assert len(found) == 1
    assert found[0]["awemeId"] == "7705352898574905203"


def test_ids_from_url_alias_keys():
    url = "https://x/aweme/v1/web/collect/?awemeid=7705352898574905203&group_id=7705352898574905204&type=0"
    ids = ids_from_url(url)
    assert "7705352898574905203" in ids and "7705352898574905204" in ids


# ---------------------------------------------------------------------------
# 2. watcher: 集合上限 / 登录判定 / CDP 类型校验 / 退出码
# ---------------------------------------------------------------------------
def test_cap_set_trims_to_limit():
    s = {str(i) for i in range(100)}
    removed = _cap_set(s, 30)
    assert removed == 70
    assert len(s) == 30
    assert _cap_set(s, 30) == 0
    assert _cap_set(s, 50) == 0


def test_check_login_requires_real_login_cookie():
    """游客态也有 ttwid/msToken 等 —— 绝不能把它们当登录, 否则全 403 还不知原因。"""
    class _Ctx:
        def __init__(self, cookies):
            self._c = cookies
        def cookies(self):
            return self._c

    guest = [{"name": "ttwid", "value": "abc"},
             {"name": "msToken", "value": "xyz"},
             {"name": "passport_csrf_token", "value": "t"}]
    assert BrowserWatcher.check_login(_Ctx(guest)) is False

    logged = guest + [{"name": "sessionid", "value": "real"}]
    assert BrowserWatcher.check_login(_Ctx(logged)) is True

    # 名字在但值为空(退出登录后残留)不算登录
    emptied = guest + [{"name": "sid_guard", "value": ""}]
    assert BrowserWatcher.check_login(_Ctx(emptied)) is False

    # Cookie 结构本身畸形也不能崩
    assert BrowserWatcher.check_login(_Ctx([None, 3, "x", {}])) is False


def test_probe_port_rejects_non_browser_json(monkeypatch):
    """端口被别的程序占了会返回数组/null/别的 JSON, 不能当成可用 CDP。"""
    w = BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()), port=9222,
                       on_aweme=lambda a, s: None, log=lambda m="": None)

    class _R:
        def __init__(self, data): self._d = data
        def read(self): return self._d
        def __enter__(self): return self
        def __exit__(self, *a): return False

    for bad in (b"[]", b"null", b'"str"', b"{}", b'{"foo":1}'):
        monkeypatch.setattr("douyin_dl.watcher.urllib.request.urlopen",
                            lambda *a, _d=bad, **k: _R(_d))
        assert w._probe_port() is None

    good = b'{"Browser":"Chrome/156","webSocketDebuggerUrl":"ws://x"}'
    monkeypatch.setattr("douyin_dl.watcher.urllib.request.urlopen",
                        lambda *a, _d=good, **k: _R(_d))
    assert isinstance(w._probe_port(), dict)


def test_exit_codes_distinct_and_nonzero_on_failure():
    """daemon 只认 7 为用户主动关; 失败出口不能是 None(=sys.exit 0)。"""
    assert EXIT_OK == 0
    assert EXIT_INTENTIONAL == 7
    for code in (EXIT_BROWSER_FAIL, EXIT_LOGIN_TIMEOUT):
        assert isinstance(code, int) and code != 0 and code != EXIT_INTENTIONAL


def test_last_advance_initialized_to_monotonic_not_zero():
    """_last_advance=0.0 会让主循环首轮立即翻页, 用户还没看第一条就跳过。"""
    w = BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()), on_aweme=lambda a, s: None)
    assert w._last_advance > 0


def test_action_batch_enqueues_up_to_eight(monkeypatch):
    """批量收藏请求(多个 id)每个都该进待补全队列, 不再只取第一个。"""
    w = BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()), on_aweme=lambda a, s: None,
                       log=lambda m="": None)
    w.logs = []
    w.log = lambda m="": w.logs.append(m)
    ids = [str(7705352898574905203 + i) for i in range(12)]
    req = json.dumps({"action": 1, "aweme_ids": ids})
    w._on_payload("action", "https://www-hj.douyin.com/aweme/v1/web/collect/", "", req)
    # 上限 8, 防异常请求刷爆
    assert len(w._pending_actions) == 8


# ---------------------------------------------------------------------------
# 3. ui: 类型归一化 / 目录穿越 / 异步重试
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,default,expect", [
    ("false", False, False), ("0", False, False), ("off", False, False),
    ("true", False, True), ("1", False, True), ("yes", False, True),
    ("", True, False),
    ("garbage", True, True),     # 未知字符串回落默认
    (None, False, False), ([], True, True), ({"x": 1}, True, True),
    (0, True, False), (1, False, True),
])
def test_as_bool_string_semantics(raw, default, expect):
    """bool("false") 是 True —— 老设置文件写成字符串会让开关反着来。"""
    assert _as_bool(raw, default) is expect


@pytest.mark.parametrize("raw,expect", [
    ({"a": 1}, {"a": 1}), (None, {}), ([], {}), ("x", {}), (3, {}),
])
def test_as_dict(raw, expect):
    assert _as_dict(raw) == expect


@pytest.mark.parametrize("raw,default,expect", [
    (3, 0.0, 3.0), ("3.5", 0.0, 3.5), (True, 0.0, 1.0),
    (float("nan"), 7.0, 7.0), (float("inf"), 7.0, 7.0),
    ("abc", 2.0, 2.0), ([1], 2.0, 2.0),
])
def test_as_number(raw, default, expect):
    assert _as_number(raw, default) == expect


def test_rel_path_rejects_traversal_and_junk():
    ws = Path(tempfile.mkdtemp()).resolve()
    assert _rel_path(ws, "../../etc/passwd") is None
    assert _rel_path(ws, "../outside.mp4") is None
    assert _rel_path(ws, 123) is None
    assert _rel_path(ws, {"x": 1}) is None
    assert _rel_path(ws, "") is None
    # 工作区内的相对路径放行
    inside = _rel_path(ws, "downloads/a.mp4")
    assert inside is not None and ws in inside.parents
    # 绝对路径(自定义下载目录)放行
    abs_p = _rel_path(ws, str(Path(tempfile.mkdtemp()) / "v.mp4"))
    assert abs_p is not None and abs_p.is_absolute()


def test_retry_is_async_and_updates_failed_list():
    """重试不该冻住面板: 立即返回 True, 后台线程完成后摘掉/更新失败项。"""
    ws = Path(tempfile.mkdtemp())
    bus = EventBus(ws, ws / "downloads", ws / ".state.json", ws / "log")
    bus.failed["7100000000000000007"] = {"why": "403", "time": 1}

    gate = {"start": time.time()}

    def slow_retry(aid):
        time.sleep(0.3)   # 模拟 refetch 抓详情
        gate["retried"] = aid
        return True

    bus.on_retry = slow_retry
    t0 = time.time()
    assert bus.retry("7100000000000000007") is True
    assert time.time() - t0 < 0.15          # 立即返回, 没等那 0.3 秒
    assert "7100000000000000007" in bus.failed   # 此刻后台还没跑完

    for _ in range(50):
        if "7100000000000000007" not in bus.failed:
            break
        time.sleep(0.02)
    assert "7100000000000000007" not in bus.failed   # 成功后摘掉
    assert gate.get("retried") == "7100000000000000007"


def test_retry_failure_keeps_item_with_updated_reason():
    ws = Path(tempfile.mkdtemp())
    bus = EventBus(ws, ws / "downloads", ws / ".state.json", ws / "log")
    bus.failed["7100000000000000008"] = {"why": "403", "time": 1}
    bus.on_retry = lambda aid: False

    assert bus.retry("7100000000000000008") is True
    for _ in range(50):
        if "重试仍失败" in str(bus.failed.get("7100000000000000008", {}).get("why", "")):
            break
        time.sleep(0.02)
    assert "重试仍失败" in bus.failed["7100000000000000008"]["why"]


def test_publish_overflow_keeps_subscribers():
    """SSE 队列满时旧版把客户端踢掉 -> 界面永久冻住; 现在丢最旧保订阅。"""
    ws = Path(tempfile.mkdtemp())
    bus = EventBus(ws, ws / "downloads", ws / ".state.json", ws / "log")
    q = bus.subscribe()
    for _ in range(200):   # maxsize 64, 远超
        bus.publish()
    assert q in bus.clients          # 订阅还在
    assert q.qsize() <= 64           # 队列有界


# ---------------------------------------------------------------------------
# 4. 主程序脚本: State.load 顶层校验
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    b"[]", b"null", b'"abc"', b"123", b"{broken json",
])
def test_state_load_malformed_top_level_becomes_empty_dict(bad, tmp_path):
    """state 写坏成非 dict 时, has/mark 不该在 list/str 上乱写。"""
    d = _load_main()
    p = tmp_path / ".state.json"
    p.write_bytes(bad)
    st = d.State(p)
    assert st.done == {}
    assert st.has("anything") is False


def test_state_load_keeps_valid_dict(tmp_path):
    d = _load_main()
    p = tmp_path / ".state.json"
    p.write_text(json.dumps({"7105352898574905203": {"res": "1080p"}}), encoding="utf-8")
    st = d.State(p)
    assert st.has("7105352898574905203") is True


def test_get_out_dir_follows_settings(tmp_path, monkeypatch):
    """面板改目录后, get_out_dir() 要跟着变(按作者归类/download_single 都靠它)。"""
    d = _load_main()
    custom = tmp_path / "myvideos"
    monkeypatch.setattr(d, "SETTINGS_FILE", tmp_path / ".settings.json")
    d.save_settings_file(d.SETTINGS_FILE, {"out_dir": str(custom)})
    assert d.get_out_dir() == custom
    assert custom.exists()   # 顺手建出来了


# ---------------------------------------------------------------------------
# 5. 无头浏览器回退链(v1.6.1 回归: 曾因只用自带 chromium 导致全部收藏下不了)
# ---------------------------------------------------------------------------
class _FakeChromium:
    """假 playwright.chromium: 记录每次 launch 参数, 按脚本决定成功/失败。"""

    def __init__(self, fail_on):
        self.fail_on = set(fail_on)      # 第几次尝试失败(0-based)
        self.calls = []

    def launch(self, **kw):
        i = len(self.calls)
        self.calls.append(kw)
        if i in self.fail_on:
            raise RuntimeError(f"launch attempt {i} failed")
        return f"browser-{i}"


class _FakePW:
    def __init__(self, fail_on):
        self.chromium = _FakeChromium(fail_on)


def test_launch_headless_falls_back_when_first_choice_fails(monkeypatch):
    """首选(系统 Chrome)失败时, 必须继续试 channel=chrome/msedge/自带, 不能一次失败就放弃。"""
    # 让 find_chrome 返回一个确定路径, 保证第一路是 executable_path
    monkeypatch.setattr("douyin_dl.watcher.find_chrome", lambda: r"C:\fake\chrome.exe")
    # 让第 0 次(系统浏览器)和第 1 次(channel=chrome)都失败, 第 2 次成功
    pw = _FakePW(fail_on={0, 1})
    got = BrowserWatcher._launch_headless(pw)
    assert got == "browser-2"
    assert len(pw.chromium.calls) >= 3          # 确实试了多次
    assert "executable_path" in pw.chromium.calls[0]
    assert pw.chromium.calls[1].get("channel") == "chrome"
    assert pw.chromium.calls[2].get("channel") == "msedge"


def test_launch_headless_all_fail_raises_clear_error(monkeypatch):
    """所有方式都起不来时, 抛清晰异常而不是静默返回 None(否则主循环白等)。"""
    monkeypatch.setattr("douyin_dl.watcher.find_chrome", lambda: None)
    pw = _FakePW(fail_on={0, 1, 2})
    with pytest.raises(RuntimeError, match="无头浏览器起不来"):
        BrowserWatcher._launch_headless(pw)


def test_launch_headless_uses_system_chrome_first(monkeypatch):
    """find_chrome 找到系统浏览器时, 第一顺位必须是它(本机实测可用)。"""
    monkeypatch.setattr("douyin_dl.watcher.find_chrome", lambda: r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    pw = _FakePW(fail_on=set())
    got = BrowserWatcher._launch_headless(pw)
    assert got == "browser-0"                   # 第一路就成了
    assert pw.chromium.calls[0]["executable_path"].endswith("chrome.exe")


# --- v1.6.2 面板交互: 收藏秒显示 / 队列 / 失败项重试与忽略 -----------------

def test_on_pending_fires_when_favorite_detected():
    """点了收藏要立刻通知面板, 不能等无头浏览器把详情抓回来(5~12 秒)。

    这是「点完收藏盯着面板半天不动」的根因: 旧版只有详情回来后才 publish。
    """
    seen: list[str] = []
    w = BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()),
                       on_pending=lambda aid: seen.append(aid),
                       log=lambda m: None)
    url = ("https://www.douyin.com/aweme/v1/web/aweme/collect/"
           "?action=1&aweme_id=7705427599274719717")
    w._on_payload("action", url, "", '{"aweme_id":"7705427599274719717"}')
    assert seen == ["7705427599274719717"]

    # 同一条重复上报(钩子重发/页面重试)不该把面板刷成两条
    w._on_payload("action", url, "", '{"aweme_id":"7705427599274719717"}')
    assert seen == ["7705427599274719717"]


def test_on_pending_not_fired_for_uncollect():
    """取消收藏不能冒出「解析中」的假进度。"""
    seen: list[str] = []
    w = BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()),
                       on_pending=lambda aid: seen.append(aid),
                       log=lambda m: None)
    url = ("https://www.douyin.com/aweme/v1/web/aweme/collect/"
           "?action=0&aweme_id=7705427599274719717")
    w._on_payload("action", url, "", '{"aweme_id":"7705427599274719717"}')
    assert seen == []


def test_add_waiting_upgrades_resolving_placeholder():
    """先插「解析中」占位, 详情回来后再补标题/来源, 队列里只留一条。"""
    ws = Path(tempfile.mkdtemp())
    bus = EventBus(ws, ws / "downloads", ws / ".state.json", ws / "log")
    bus.add_waiting("7705427599274719717", source="解析中")
    assert len(bus.waiting) == 1 and bus.waiting[0]["source"] == "解析中"

    bus.add_waiting("7705427599274719717", title="耳机没了", author="小明", source="收藏")
    assert len(bus.waiting) == 1                     # 没有变成两条
    row = bus.waiting[0]
    assert row["title"] == "耳机没了" and row["author"] == "小明"
    assert row["source"] == "收藏"                   # 占位的「解析中」被替换
    # 再补一次空信息不该把已有标题清掉
    bus.add_waiting("7705427599274719717")
    assert bus.waiting[0]["title"] == "耳机没了"


def test_retry_marks_item_retrying_immediately():
    """点重试要在一百毫秒内看到变化, 否则用户以为按钮坏了会连点。"""
    ws = Path(tempfile.mkdtemp())
    bus = EventBus(ws, ws / "downloads", ws / ".state.json", ws / "log")
    aid = "7100000000000000009"
    bus.failed[aid] = {"id": aid, "why": "403", "time": 1}

    release = {"go": False}

    def slow(a):
        while not release["go"]:
            time.sleep(0.01)
        return True, ""

    bus.on_retry = slow
    assert bus.retry(aid) is True
    assert bus.failed[aid].get("retrying") is True   # 立刻可见的反馈

    # 已经在重试时再点一次, 不叠加第二个后台任务
    assert bus.retry(aid) is True
    release["go"] = True
    for _ in range(60):
        if aid not in bus.failed:
            break
        time.sleep(0.02)
    assert aid not in bus.failed


def test_retry_failure_uses_specific_reason():
    """抓不到详情时把具体原因写回失败项, 而不是同一句「重试仍失败」。"""
    ws = Path(tempfile.mkdtemp())
    bus = EventBus(ws, ws / "downloads", ws / ".state.json", ws / "log")
    aid = "7100000000000000010"
    bus.failed[aid] = {"id": aid, "why": "403", "time": 1}
    bus.on_retry = lambda a: (False, "拿不到视频详情(可能已删除/私密)")
    assert bus.retry(aid) is True
    for _ in range(60):
        if "拿不到视频详情" in str(bus.failed.get(aid, {}).get("why", "")):
            break
        time.sleep(0.02)
    why = bus.failed[aid]["why"]
    assert "拿不到视频详情" in why and "403" in why     # 原因叠加, 原信息不丢
    assert bus.failed[aid].get("retrying") is False     # 可再次重试


def test_dismiss_removes_failed_item_only():
    """叉掉失败项只改面板列表, 不碰已下载文件、不删下载记录。"""
    ws = Path(tempfile.mkdtemp())
    out = ws / "downloads"
    out.mkdir()
    video = out / "a.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
    state = ws / ".state.json"
    state.write_text(json.dumps({"7100000000000000011": {"file": "downloads/a.mp4"}}),
                     encoding="utf-8")

    bus = EventBus(ws, out, state, ws / "log")
    bus.failed["7100000000000000011"] = {"id": "7100000000000000011", "why": "403", "time": 1}

    assert bus.dismiss("7100000000000000011") is True
    assert "7100000000000000011" not in bus.failed
    assert video.exists()                                   # 文件还在
    assert "7100000000000000011" in json.loads(state.read_text(encoding="utf-8"))
    assert bus.dismiss("7100000000000000011") is False      # 幂等, 不抛异常


# --- v1.6.3 启动后第一个收藏必失败的竞态 -------------------------------

def test_ensure_hl_worker_keeps_pending_requests(monkeypatch):
    """起线程时只清退出信号, 已在队列里的真请求必须留着。

    回归: v1.6 把"排空残留"放在线程里做, 而 _resolve_pending 是
    「先起线程 → 再取 cookie → 再投递」, 线程跑到排空时新请求已经入队,
    被当成残留丢掉并回 None, 主循环 2 秒内就判定"拿不到视频信息"。
    表现: 每次启动后第一个收藏必失败, 之后的都正常。
    """
    import queue as _q

    monkeypatch.setattr(BrowserWatcher, "_hl_loop", lambda self: None)
    w = BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()), log=lambda m: None)
    w._hl_thread = None
    w._hl_in = _q.Queue()
    w._hl_out = _q.Queue()
    w._hl_in.put(None)                                # 上次留下的退出信号
    w._hl_in.put(("7705427599274719717", [{"name": "sessionid"}]))   # 已投递的真请求

    w._ensure_hl_worker()

    left = []
    while True:
        try:
            left.append(w._hl_in.get_nowait())
        except _q.Empty:
            break
    assert left == [("7705427599274719717", [{"name": "sessionid"}])]


def test_requeue_clears_stale_failed_entry():
    """重新入队(重新收藏/重试成功)要立刻摘掉失败列表里的旧记录。

    否则重新收藏下载成功之后, 面板"下载失败"里还挂着同一条, 看着像没修好。
    """
    ws = Path(tempfile.mkdtemp())
    bus = EventBus(ws, ws / "downloads", ws / ".state.json", ws / "log")
    aid = "7706764113468247336"
    bus.fail(aid, "抓不到视频详情")
    assert aid in bus.failed

    bus.add_waiting(aid, source="解析中")
    assert aid not in bus.failed          # 旧失败记录被摘掉
    assert any(x["id"] == aid for x in bus.waiting)


# ---------------------------------------------------------------------------
# 6. v1.7.1: 坏页面看门狗 (Page.evaluate 对坏页永久阻塞, 把主循环钉死)
# ---------------------------------------------------------------------------
def _make_watcher():
    return BrowserWatcher(profile_dir=Path(tempfile.mkdtemp()), log=lambda m: None)


def test_install_hook_skips_non_site_and_blank_urls():
    """空 URL / 非站点页绝不能碰 page.evaluate —— 这类页面会永久阻塞主循环。"""
    w = _make_watcher()

    class _Blank:
        url = ""
        def evaluate(self, *_a, **_k): raise AssertionError("不该对空 URL 调 evaluate")
    class _About:
        url = "about:blank"
        def evaluate(self, *_a, **_k): raise AssertionError("不该对 about: 调 evaluate")
    class _Other:
        url = "https://example.com/foo"
        def evaluate(self, *_a, **_k): raise AssertionError("不该对非站点页调 evaluate")

    for pg in (_Blank(), _About(), _Other()):
        assert w._install_hook(pg) is False


def test_install_hook_probe_dead_page_blacklists_without_evaluate():
    """探活失败(渲染进程死)的站点页要拉黑, 且绝不调用 page.evaluate。"""
    w = _make_watcher()
    site_url = "https://www.kuaishou.com/new-reco"
    calls = {"evaluate": 0}

    class _DeadSite:
        url = site_url
        def evaluate(self, *_a, **_k):
            calls["evaluate"] += 1
            raise AssertionError("探活没通过就不该 evaluate")

    # 探活走 CDP, 这里用假实现直接返回 False(模拟渲染进程无响应)
    w._page_alive = lambda page: False
    assert w._install_hook(_DeadSite()) is False
    assert calls["evaluate"] == 0
    assert w._page_blacklisted(site_url) is True


def test_install_hook_unblacklists_after_success():
    """evaluate 成功装好钩子后要解禁该 URL(清掉任何残留的拉黑记录)。"""
    w = _make_watcher()
    site_url = "https://www.douyin.com/?recommend=1"
    # 注意: 已在拉黑期的页面, _install_hook 开头就会 return False, 根本走不到
    # 探活; 解禁只发生在「拉黑过期后重新探活成功」这一路。这里直接断言成功路径
    # 会清掉记录、返回 True。
    w._page_alive = lambda page: True

    class _GoodSite:
        url = site_url
        def evaluate(self, js=None, *_a, **_k):
            if js == "() => window.__DY_HOOK_VER__ || 0":
                from douyin_dl.watcher import HOOK_VER
                return HOOK_VER                  # 与 HOOK_VER 相等, 不重装
            raise AssertionError("不该再装 HOOK_JS")
    assert w._install_hook(_GoodSite()) is True
    assert w._page_blacklisted(site_url) is False


def test_install_hook_skips_still_blacklisted_page():
    """拉黑期内的站点页直接跳过(不探活、不 evaluate), 等过期后再看。"""
    w = _make_watcher()
    site_url = "https://www.kuaishou.com/new-reco"
    w._bad_pages[site_url] = time.time() + 60
    probed = {"n": 0}
    w._page_alive = lambda page: (probed.__setitem__("n", probed["n"] + 1) or True)

    class _Site:
        url = site_url
        def evaluate(self, *_a, **_k):
            raise AssertionError("拉黑期内不该 evaluate")
    assert w._install_hook(_Site()) is False
    assert probed["n"] == 0                    # 连探活都省了


def test_pump_prefers_healthy_site_page_over_bad_first_page():
    """pump 要跳过坏页/非站点页, 挑一个健康的站点页来派发事件, 而不是永远 pages[0]。"""
    w = _make_watcher()
    events = []

    class _Bad:
        url = ""
        def wait_for_timeout(self, *_a, **_k):
            raise AssertionError("不该对空 URL 页 wait_for_timeout")
    class _Good:
        url = "https://www.douyin.com/?recommend=1"
        def wait_for_timeout(self, ms):
            events.append(ms)

    class _Ctx:
        pages = [_Bad(), _Good()]

    w.pump(_Ctx())
    assert events == [1]


def test_pump_falls_back_to_browser_cdp_when_no_site_page():
    """一个站点页都没有时, 退回 browser 级 CDP 派发事件, 而不是卡死。"""
    w = _make_watcher()
    got = {"version": False}

    class _Cdp:
        def send(self, method):
            if method == "Browser.getVersion":
                got["version"] = True
                return {}
            raise AssertionError(f"意外方法 {method}")
        def detach(self):
            pass
    class _Browser:
        def new_browser_cdp_session(self):
            return _Cdp()
    class _Other:
        url = "https://example.com/x"
        def wait_for_timeout(self, *_a, **_k):
            raise AssertionError("不该对非站点页 wait_for_timeout")
    class _Ctx:
        pages = [_Other()]

    w._browser = _Browser()
    w.pump(_Ctx())
    assert got["version"] is True


# ---------------------------------------------------------------------------
# 7. v1.7.2: 半死页 (page.url 空但 CDP 层活着) 的 raw 通道
# ---------------------------------------------------------------------------
def test_page_url_falls_back_to_cdp_for_halfdead_page():
    """page.url 空时, 用 CDP Target.getTargetInfo 拿真实 URL, 识别出站点页。"""
    w = _make_watcher()
    w._ctx = object()
    tid = "56B9D255FA6F"

    class _HalfDead:
        url = ""

    w._page_debugger_id = lambda page: tid
    import urllib.request as _ur, json as _json
    orig = _ur.urlopen
    class _Resp:
        def read(self):
            return _json.dumps([{"id": tid, "url": "https://www.kuaishou.com/new-reco"}]).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    try:
        _ur.urlopen = lambda *a, **k: _Resp()
        assert w._page_url(_HalfDead()) == "https://www.kuaishou.com/new-reco"
    finally:
        _ur.urlopen = orig


def test_install_hook_halfdead_page_uses_raw_channel_not_evaluate():
    """半死页(站点 URL 但 Playwright url 空)走 CDP 注入 + raw binding, 绝不 evaluate。"""
    w = _make_watcher()
    w._ctx = object()
    site_url = "https://www.kuaishou.com/new-reco"
    tid = "56B9D255FA6F"

    class _HalfDead:
        url = ""
        def evaluate(self, *_a, **_k):
            raise AssertionError("半死页绝不走 page.evaluate")

    w._page_alive = lambda page: True
    w._page_debugger_id = lambda page: tid
    # _page_url 对半死页也要能拿到站点 URL(走 CDP 兜底), 这里直接给真实 URL
    w._page_url = lambda page: site_url
    calls = {"cdp_eval": 0, "raw_inject": 0, "ensure_raw": 0}
    w._cdp_eval = lambda tid, expr: (calls.__setitem__("cdp_eval", calls["cdp_eval"] + 1) or 0)
    def _raw_cdp(tid_, method, params=None, timeout=6.0):
        if method == "Runtime.evaluate":
            calls["raw_inject"] += 1
        return {}
    w._raw_cdp = _raw_cdp
    w._ensure_raw_binding = lambda page: (calls.__setitem__("ensure_raw", calls["ensure_raw"] + 1) or True)

    assert w._install_hook(_HalfDead()) is True
    assert calls["ensure_raw"] == 1
    assert calls["cdp_eval"] == 1
    assert calls["raw_inject"] == 1      # HOOK_JS 通过 CDP 注入


def test_hook_js_prefers_raw_report_binding():
    """HOOK_JS 的 send() 优先 __dyReportRaw, 半死页能走 raw 通道。"""
    from douyin_dl.watcher import HOOK_JS
    # send() 里先试 __dyReportRaw, 再退回 __dyReport
    assert "__dyReportRaw" in HOOK_JS
    assert "window.__dyReport(payload)" in HOOK_JS or "window.__dyReport(" in HOOK_JS
    # HOOK_VER 必须两侧一致
    from douyin_dl.watcher import HOOK_VER
    assert f"const HOOK_VER = {HOOK_VER};" in HOOK_JS


def test_ensure_raw_binding_passes_tid_to_worker_thread():
    """v1.7.2 的回归: _ensure_raw_binding 起线程必须传 args=(tid,)。

    曾经漏写 args 导致 _raw_binding_worker() missing 1 required positional
    argument: 'tid', raw 通道线程一启动就崩, 半死页收藏事件永远传不回来。
    这里用假 Thread 抓取传给 target 的参数, 锁定这个契约。
    """
    w = _make_watcher()
    tid = "B5E843C5DE7E9B90784F66F0F5F61340"

    class _Page:
        pass

    captured = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), name="", daemon=None):
            captured["target"] = target
            captured["args"] = args
            captured["name"] = name
            captured["daemon"] = daemon
        def start(self):
            captured["started"] = True
        def is_alive(self):
            return False

    w._page_debugger_id = lambda page: tid
    import threading as _th
    orig = _th.Thread
    try:
        _th.Thread = _FakeThread
        assert w._ensure_raw_binding(_Page()) is True
    finally:
        _th.Thread = orig

    # 核心契约: 线程 target 是 _raw_binding_worker, 且 args 恰好是 (tid,)
    assert captured.get("target") == w._raw_binding_worker
    assert captured.get("args") == (tid,)
    assert captured.get("started") is True
    # 名字里带 targetId 前 8 位, 便于排查
    assert captured.get("name") == f"dy-raw-{tid[:8]}"
    # 注册表里已记录该线程
    assert w._raw_workers.get(tid) is not None


def test_hook_all_pages_covers_orphan_halfdead_page_via_cdp():
    """v1.7.2 回归: Playwright 看不见的孤儿半死页也要经 CDP 补漏注入钩子。

    半死页(从打开起 attach 就失败)不进 ctx.pages、不触发 ctx.on("page"),
    所以 _hook_all_pages 必须直接问 CDP /json/list 拿全部 page target, 对
    Playwright 漏掉的站点页走纯 CDP 注入 + raw binding。这里用假 ctx + 假
    urlopen 模拟这个场景。
    """
    w = _make_watcher()
    tid = "ED1A707AA26C07E7C89EF19F0372AE66"
    site_url = "https://www.kuaishou.com/new-reco"

    # Playwright 侧完全看不见这个页: ctx.pages 里没有任何对应 page
    class _Ctx:
        pages = []

    calls = {"probe": 0, "ensure_raw": 0, "cdp_eval": 0, "raw_inject": 0}
    w._cdp_probe = lambda t: (calls.__setitem__("probe", calls["probe"] + 1) or True)
    w._ensure_raw_binding_by_tid = lambda t: (calls.__setitem__("ensure_raw", calls["ensure_raw"] + 1) or True)
    w._cdp_eval = lambda t, expr: (calls.__setitem__("cdp_eval", calls["cdp_eval"] + 1) or 0)
    def _raw_cdp(t, method, params=None, timeout=6.0):
        if method == "Runtime.evaluate":
            calls["raw_inject"] += 1
        return {}
    w._raw_cdp = _raw_cdp

    import urllib.request as _ur, json as _json
    class _Resp:
        def read(self):
            return _json.dumps([
                {"id": tid, "type": "page", "url": site_url,
                 "webSocketDebuggerUrl": "ws://x"},
            ]).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    orig = _ur.urlopen
    try:
        _ur.urlopen = lambda *a, **k: _Resp()
        n = w._hook_all_pages(_Ctx())
    finally:
        _ur.urlopen = orig

    assert n == 1
    assert calls["probe"] == 1
    assert calls["ensure_raw"] == 1
    assert calls["cdp_eval"] == 1
    assert calls["raw_inject"] == 1        # HOOK_JS 通过纯 CDP 注入孤儿页
