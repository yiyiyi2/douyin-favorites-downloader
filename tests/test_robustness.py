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
