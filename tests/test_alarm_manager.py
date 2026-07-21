import datetime
import time

import pytest

import alarms.manager as manager_module
from alarms.manager import AlarmManager
from live_trader.engine import _format_market_scope


class FakeAlarmChannel:
    def __init__(self):
        self.text_calls = []
        self.exception_calls = []
        self.status_calls = []

    def push_text(self, content: str, level: str = 'INFO'):
        self.text_calls.append((content, level))

    def push_exception(self, context: str, error):
        self.exception_calls.append((context, str(error)))

    def push_status(self, status: str, detail: str = ''):
        self.status_calls.append((status, detail))


def _wait_until(predicate, timeout_sec=2.0):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def fresh_alarm_manager(monkeypatch):
    AlarmManager._instance = None

    # 单测中禁用死信注册，避免重复绑定 atexit/signal 干扰
    monkeypatch.setattr(manager_module.AlarmManager, "_register_dead_letter_handlers", lambda self: None)
    monkeypatch.setattr(manager_module.config, "ALARMS_ENABLED", False)

    mgr = AlarmManager()
    # 生产默认 60 秒窗口，异常单测缩短到 1 秒以避免等待过久
    mgr._exception_aggregation_window_seconds = 1
    yield mgr

    timer = getattr(mgr, "_exception_flush_timer", None)
    if timer:
        timer.cancel()
    timer = getattr(mgr, "_exception_cooldown_timer", None)
    if timer:
        timer.cancel()
    timer = getattr(mgr, "_daily_schedule_health_timer", None)
    if timer:
        timer.cancel()
    AlarmManager._instance = None


def test_push_text_dispatches_immediately(fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]

    mgr.push_text("订单被拒绝: SHSE.600000", level="WARNING")
    assert _wait_until(lambda: len(fake.text_calls) == 1), "push_text 应立即异步发送。"

    content, level = fake.text_calls[0]
    assert level == "WARNING", "合并推送应保留原始报警级别。"
    assert "订单被拒绝: SHSE.600000" in content, "合并推送应保留原始报警内容。"
    assert "重复次数" not in content, "push_text 不应再做文本聚合去重。"


def test_alarm_manager_auto_enables_channels_when_webhook_is_configured(monkeypatch):
    AlarmManager._instance = None
    monkeypatch.setattr(manager_module.AlarmManager, "_register_dead_letter_handlers", lambda self: None)
    monkeypatch.setattr(manager_module.config, "ALARMS_ENABLED", None)
    monkeypatch.setattr(manager_module.config, "DINGTALK_WEBHOOK", "")
    monkeypatch.setattr(manager_module.config, "WECOM_WEBHOOK", "https://example.invalid/wecom")

    mgr = AlarmManager()

    try:
        assert [alarm.__class__.__name__ for alarm in mgr.alarms] == ["WeComAlarm"]
    finally:
        AlarmManager._instance = None


def test_alarm_manager_explicit_false_disables_channels_even_with_webhook(monkeypatch):
    AlarmManager._instance = None
    monkeypatch.setattr(manager_module.AlarmManager, "_register_dead_letter_handlers", lambda self: None)
    monkeypatch.setattr(manager_module.config, "ALARMS_ENABLED", False)
    monkeypatch.setattr(manager_module.config, "DINGTALK_WEBHOOK", "")
    monkeypatch.setattr(manager_module.config, "WECOM_WEBHOOK", "https://example.invalid/wecom")

    mgr = AlarmManager()

    try:
        assert mgr.alarms == []
    finally:
        AlarmManager._instance = None


def test_runtime_context_status_detail_contains_market_scope(fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]

    market_scope = "selector=us_growth_selector"
    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="7497",
        strategy="my_strategy",
        params={"lookback": 20},
        market_scope=market_scope,
    )
    mgr.push_status("STARTED", "Booting")

    assert _wait_until(lambda: len(fake.status_calls) == 1), "状态推送应到达报警通道。"
    status, detail = fake.status_calls[0]
    assert status == "STARTED [IB_BROKER:7497]", "状态标题应包含 broker/连接身份上下文。"
    assert f"Market: {market_scope}" in detail, "状态详情应包含 selector/symbols 市场上下文。"


def test_push_exception_aggregates_same_error_with_count(fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]

    mgr.push_exception("GM Kernel Error", "Code: 1100, Msg: 交易消息服务连接失败")
    mgr.push_exception("GM Kernel Error", "Code: 1100, Msg: 交易消息服务连接失败")

    assert fake.exception_calls == [], "聚合窗口内不应立即发送重复异常报警。"
    assert _wait_until(lambda: len(fake.exception_calls) == 1), "聚合窗口到期后应触发一次异常合并推送。"

    context, error_text = fake.exception_calls[0]
    assert "GM Kernel Error" in context, "异常合并推送应保留原始模块上下文。"
    assert "Code: 1100, Msg: 交易消息服务连接失败" in error_text, "异常合并推送应保留原始错误内容。"
    assert "重复次数: 2" in error_text, "相同异常应附带聚合数量。"


def test_push_text_duplicate_messages_are_not_buffered(fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]

    mgr.push_text("风控告警: 持仓超限", level="WARNING")
    mgr.push_text("风控告警: 持仓超限", level="WARNING")

    assert _wait_until(lambda: len(fake.text_calls) == 2, timeout_sec=1.0), "重复 text 应逐条立即发送。"

    content, level = fake.text_calls[0]
    assert level == "WARNING", "冷却后推送应保留原始级别。"
    assert "风控告警: 持仓超限" in content, "冷却后推送应保留原始内容。"
    assert "重复次数" not in content, "text 不应附带聚合计数。"


def test_exception_log_cooldown_merges_counts_across_windows(fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]
    mgr._exception_aggregation_window_seconds = 0.2
    mgr._cooldown_base_delay_seconds = 0.2
    mgr._cooldown_max_delay_seconds = 0.4
    mgr._cooldown_reset_window_seconds = 5

    error_text = "Code: 1100, Msg: 交易消息服务连接失败"
    mgr.push_exception("GM Kernel Error", error_text)
    assert _wait_until(lambda: len(fake.exception_calls) == 1, timeout_sec=1.0), "首轮异常窗口到期后应立即推送。"

    mgr.push_exception("GM Kernel Error", error_text)
    mgr.push_exception("GM Kernel Error", error_text)
    time.sleep(0.25)
    assert len(fake.exception_calls) == 1, "异常冷却期内不应立即推送第二轮重复告警。"
    assert _wait_until(lambda: len(fake.exception_calls) == 2, timeout_sec=1.5), "异常冷却到期后应推送累计数量。"

    context, merged_error = fake.exception_calls[1]
    assert "GM Kernel Error" in context, "冷却后异常推送应保留原始模块上下文。"
    assert error_text in merged_error, "冷却后异常推送应保留原始错误内容。"
    assert "重复次数: 2" in merged_error, "异常冷却期间应累计重复数量后再推送。"


def test_format_market_scope_prefers_selector_only():
    scope = _format_market_scope(selection="cn_topn_selector", symbols=["SHSE.510300", "SZSE.159915"])
    assert scope == "selector=cn_topn_selector"


def test_parse_schedule_alarm_window_supports_mixed_units(fresh_alarm_manager):
    mgr = fresh_alarm_manager

    before_seconds, after_seconds = mgr._parse_schedule_alarm_window("1h:30s")
    assert before_seconds == pytest.approx(3600.0)
    assert after_seconds == pytest.approx(30.0)

    before_seconds, after_seconds = mgr._parse_schedule_alarm_window("5m:15m")
    assert before_seconds == pytest.approx(300.0)
    assert after_seconds == pytest.approx(900.0)


def test_schedule_alarm_window_uses_global_default_when_runtime_value_missing(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    monkeypatch.setattr(manager_module.config, "LIVE_SCHEDULE_ALARM_WINDOW", "30m:15m")

    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="7497",
        strategy="my_strategy",
        params={},
        market_scope="symbols=QQQ",
        schedule_rule="1d:15:45:00",
        alarm_window=None,
    )

    assert mgr._schedule_alarm_window_before_seconds == pytest.approx(1800.0)
    assert mgr._schedule_alarm_window_after_seconds == pytest.approx(900.0)


def test_daily_schedule_pushes_health_status_thirty_minutes_before_every_day(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]
    now = {"value": datetime.datetime(2026, 7, 18, 14, 0, 0)}
    timers = []

    class FakeTimer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.daemon = False
            self.started = False
            self.cancelled = False
            timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    monkeypatch.setattr(manager_module.threading, "Timer", FakeTimer)
    monkeypatch.setattr(mgr, "_current_schedule_alarm_time", lambda: now["value"])

    mgr.set_runtime_context(
        broker="gm_broker",
        conn_id="demo",
        strategy="my_strategy",
        params={},
        schedule_rule="1d:14:45:00",
    )

    assert len(timers) == 1
    assert timers[0].started is True
    assert timers[0].delay == pytest.approx(15 * 60)

    now["value"] = datetime.datetime(2026, 7, 18, 14, 15, 1)
    timers[0].callback()

    assert len(timers) == 2
    assert timers[1].delay == pytest.approx(24 * 60 * 60 - 1)
    assert _wait_until(lambda: len(fake.status_calls) == 1)
    status, detail = fake.status_calls[0]
    assert status == "ALIVE [GM_BROKER:demo]"
    assert "Next daily schedule: 2026-07-18 14:45:00" in detail


def test_interval_schedule_does_not_create_daily_health_timer(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    mgr.alarms = [FakeAlarmChannel()]
    timers = []

    class FakeTimer:
        def __init__(self, delay, callback):
            timers.append((delay, callback))

        def start(self):
            return None

        def cancel(self):
            return None

        def is_alive(self):
            return False

    monkeypatch.setattr(manager_module.threading, "Timer", FakeTimer)
    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="demo",
        strategy="my_strategy",
        params={},
        schedule_rule="5m:09:30:00",
    )

    assert timers == []


def test_supervised_worker_exit_suppresses_internal_lifecycle_im(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]
    monkeypatch.setattr(manager_module, "is_live_worker_process", lambda: True)

    mgr._on_exit()

    assert fake.status_calls == []


def test_supervised_worker_leaves_signal_handling_to_parent(monkeypatch):
    AlarmManager._instance = None
    registered_atexit = []
    registered_signals = []
    monkeypatch.setattr(manager_module.config, "ALARMS_ENABLED", False)
    monkeypatch.setattr(manager_module, "is_live_worker_process", lambda: True)
    monkeypatch.setattr(
        manager_module.atexit,
        "register",
        lambda callback: registered_atexit.append(callback),
    )
    monkeypatch.setattr(
        manager_module.signal,
        "signal",
        lambda sig, handler: registered_signals.append((sig, handler)),
    )

    try:
        AlarmManager()
        assert len(registered_atexit) == 1
        assert registered_signals == []
    finally:
        AlarmManager._instance = None


def test_schedule_alarm_window_zero_zero_means_unrestricted(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]

    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="7497",
        strategy="my_strategy",
        params={},
        market_scope="symbols=QQQ",
        schedule_rule="1d:15:45:00",
        alarm_window="0:0",
    )
    monkeypatch.setattr(
        mgr,
        "_current_schedule_alarm_time",
        lambda: datetime.datetime(2026, 4, 14, 3, 0, 0),
    )

    mgr.push_text("夜间报警仍应放行", level="WARNING")
    assert _wait_until(lambda: len(fake.text_calls) == 1), "0:0 应视为不限制报警窗口。"


def test_schedule_alarm_window_blocks_text_outside_window(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]

    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="7497",
        strategy="my_strategy",
        params={},
        market_scope="symbols=QQQ",
        schedule_rule="1d:15:45:00",
        alarm_window="30m:15m",
    )
    monkeypatch.setattr(
        mgr,
        "_current_schedule_alarm_time",
        lambda: datetime.datetime(2026, 4, 14, 14, 0, 0),
    )

    mgr.push_text("窗口外报警", level="WARNING")
    time.sleep(0.2)
    assert fake.text_calls == [], "正式 schedule 窗口外的 text 报警不应推送到 IM。"


def test_schedule_alarm_window_allows_text_inside_interval_slot(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]

    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="7497",
        strategy="my_strategy",
        params={},
        market_scope="symbols=QQQ",
        schedule_rule="5m:09:30:00",
        alarm_window="1m:30s",
    )
    monkeypatch.setattr(
        mgr,
        "_current_schedule_alarm_time",
        lambda: datetime.datetime(2026, 4, 14, 9, 34, 20),
    )

    mgr.push_text("slot 前 40 秒报警", level="WARNING")
    assert _wait_until(lambda: len(fake.text_calls) == 1), "固定频率 slot 窗口内的报警应正常推送。"


def test_schedule_alarm_window_blocks_exception_intake_outside_window(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]
    mgr._exception_aggregation_window_seconds = 0.2

    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="7497",
        strategy="my_strategy",
        params={},
        market_scope="symbols=QQQ",
        schedule_rule="1d:15:45:00",
        alarm_window="30m:15m",
    )
    monkeypatch.setattr(
        mgr,
        "_current_schedule_alarm_time",
        lambda: datetime.datetime(2026, 4, 14, 20, 0, 0),
    )

    mgr.push_exception("IB Kernel Error", "Error 1100")
    time.sleep(0.5)
    assert fake.exception_calls == [], "窗口外异常不应进入 IM 聚合/推送流程。"


def test_schedule_alarm_window_allows_plan_messages_outside_window(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]

    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="7497",
        strategy="my_strategy",
        params={},
        market_scope="symbols=QQQ",
        schedule_rule="1d:15:45:00",
        alarm_window="30m:15m",
    )
    monkeypatch.setattr(
        mgr,
        "_current_schedule_alarm_time",
        lambda: datetime.datetime(2026, 4, 14, 2, 0, 0),
    )

    mgr.push_plan("#### 调仓计划生成")
    assert _wait_until(lambda: len(fake.text_calls) == 1), "plan 标签消息应绕过 schedule 报警窗口。"


def test_schedule_alarm_window_allows_lifecycle_status_outside_window(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]
    assert mgr._resolve_status_alarm_tag("ALIVE") == manager_module.BaseAlarm.TAG_LIFECYCLE

    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="7497",
        strategy="my_strategy",
        params={},
        market_scope="symbols=QQQ",
        schedule_rule="1d:15:45:00",
        alarm_window="30m:15m",
    )
    monkeypatch.setattr(
        mgr,
        "_current_schedule_alarm_time",
        lambda: datetime.datetime(2026, 4, 14, 2, 0, 0),
    )

    mgr.push_status("DEAD", "Process killed by SIGTERM")
    assert _wait_until(lambda: len(fake.status_calls) == 1), "生命周期状态消息应绕过 schedule 报警窗口。"


def test_schedule_alarm_window_still_blocks_non_lifecycle_status_outside_window(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]

    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="7497",
        strategy="my_strategy",
        params={},
        market_scope="symbols=QQQ",
        schedule_rule="1d:15:45:00",
        alarm_window="30m:15m",
    )
    monkeypatch.setattr(
        mgr,
        "_current_schedule_alarm_time",
        lambda: datetime.datetime(2026, 4, 14, 2, 0, 0),
    )

    mgr.push_status("INFO", "GM Session Shutdown (Preparing to Restart)")
    time.sleep(0.2)
    assert fake.status_calls == [], "普通 INFO 状态不应因为 status 接口而自动绕过报警窗口。"


def test_schedule_api_unavailable_alarm_covers_prewarm_and_run_slots(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]
    monkeypatch.setattr(manager_module.config, "LIVE_SCHEDULE_PREWARM_LEAD", "60s")

    mgr.set_runtime_context(
        broker="ib_broker",
        conn_id="7497",
        strategy="my_strategy",
        params={},
        market_scope="symbols=QQQ",
        schedule_rule="1d:15:45:00",
        alarm_window="5m:5m",
    )

    prewarm_sent = mgr.push_schedule_api_unavailable(
        "IBBroker",
        "connect failed",
        now=datetime.datetime(2026, 5, 5, 15, 44, 30),
    )
    prewarm_repeat = mgr.push_schedule_api_unavailable(
        "IBBroker",
        "connect failed again",
        now=datetime.datetime(2026, 5, 5, 15, 44, 35),
    )
    run_sent = mgr.push_schedule_api_unavailable(
        "IBBroker",
        "connect failed",
        now=datetime.datetime(2026, 5, 5, 15, 45, 5),
    )
    run_repeat = mgr.push_schedule_api_unavailable(
        "IBBroker",
        "connect failed again",
        now=datetime.datetime(2026, 5, 5, 15, 45, 10),
    )

    assert [item["event"] for item in prewarm_sent] == ["prewarm"]
    assert prewarm_repeat == []
    assert [item["event"] for item in run_sent] == ["run"]
    assert run_repeat == []
    assert _wait_until(lambda: len(fake.text_calls) == 2)
    assert all(item[1] == "ERROR" for item in fake.text_calls)
    assert "schedule prewarm may be missed" in fake.text_calls[0][0]
    assert "strategy run may be missed" in fake.text_calls[1][0]


def test_schedule_api_unavailable_alarm_is_sent_every_day(fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]
    mgr.set_runtime_context(
        broker="gm_broker",
        conn_id="demo",
        strategy="my_strategy",
        params={},
        schedule_rule="1d:14:45:00",
    )

    sent = mgr.push_schedule_api_unavailable(
        "GmBroker",
        "GM terminal unavailable",
        now=datetime.datetime(2026, 7, 18, 14, 45, 0),
    )

    assert [item["event"] for item in sent] == ["run"]
    assert _wait_until(lambda: len(fake.text_calls) == 1)
    assert fake.text_calls[0][1] == "ERROR"


def test_schedule_api_unavailable_alarm_covers_interval_slot_body(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]
    monkeypatch.setattr(manager_module.config, "LIVE_SCHEDULE_PREWARM_LEAD", "60s")

    mgr.set_runtime_context(
        broker="gm_broker",
        conn_id="demo",
        strategy="my_strategy",
        params={},
        market_scope="symbols=SHSE.600000",
        schedule_rule="5m:09:30:00",
        alarm_window="0:0",
    )

    sent = mgr.push_schedule_api_unavailable(
        "GmBroker",
        "terminal not started",
        now=datetime.datetime(2026, 5, 5, 9, 33, 0),
    )

    assert [item["event"] for item in sent] == ["run"]
    assert sent[0]["slot_key"] == "2026-05-05 09:30:00"
    assert _wait_until(lambda: len(fake.text_calls) == 1)
    assert "GmBroker Error" in fake.text_calls[0][0]


def test_dead_signal_does_not_emit_stopped_again_on_exit(monkeypatch, fresh_alarm_manager):
    mgr = fresh_alarm_manager
    fake = FakeAlarmChannel()
    mgr.alarms = [fake]

    monkeypatch.setattr(manager_module.sys, "exit", lambda code=0: None)

    mgr._signal_handler(manager_module.signal.SIGTERM, None)
    time.sleep(0.1)
    mgr._on_exit()
    time.sleep(0.1)

    assert len(fake.status_calls) == 1, "DEAD 已发送后，atexit 不应再补发 STOPPED。"
    status, detail = fake.status_calls[0]
    assert status == "DEAD"
    assert "SIGTERM" in detail
