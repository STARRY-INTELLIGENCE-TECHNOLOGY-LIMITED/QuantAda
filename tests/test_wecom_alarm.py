import alarms.wecom_alarm as wecom_module


class _FakeResponse:
    status_code = 200
    text = "ok"

    @staticmethod
    def json():
        return {"errcode": 0, "errmsg": "ok"}


def test_wecom_push_text_sends_markdown_payload(monkeypatch):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=0):
        sent.append((url, json, headers, timeout))
        return _FakeResponse()

    monkeypatch.setattr(wecom_module.config, "WECOM_WEBHOOK", "https://example.invalid/wecom", raising=False)
    monkeypatch.setattr(wecom_module.requests, "post", fake_post)

    alarm = wecom_module.WeComAlarm()
    alarm.push_text("hello text")

    assert len(sent) == 1
    _, payload, _, _ = sent[0]
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["content"] == "hello text"


def test_wecom_push_status_still_sends_payload(monkeypatch):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=0):
        sent.append((url, json, headers, timeout))
        return _FakeResponse()

    monkeypatch.setattr(wecom_module.config, "WECOM_WEBHOOK", "https://example.invalid/wecom", raising=False)
    monkeypatch.setattr(wecom_module.requests, "post", fake_post)

    alarm = wecom_module.WeComAlarm()
    alarm.push_status("STARTED [GM_BROKER:demo]", "detail")
    alarm.push_status("ALIVE [GM_BROKER:demo]", "detail")

    assert len(sent) == 2
    _, started_payload, _, _ = sent[0]
    _, alive_payload, _, _ = sent[1]
    assert started_payload["msgtype"] == "markdown"
    assert "系统状态: STARTED [GM_BROKER:demo]" in started_payload["markdown"]["content"]
    assert "✅ 系统状态: ALIVE [GM_BROKER:demo]" in alive_payload["markdown"]["content"]


def test_wecom_push_dead_status_with_context_keeps_dead_style(monkeypatch):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=0):
        sent.append((url, json, headers, timeout))
        return _FakeResponse()

    monkeypatch.setattr(wecom_module.config, "WECOM_WEBHOOK", "https://example.invalid/wecom", raising=False)
    monkeypatch.setattr(wecom_module.requests, "post", fake_post)

    alarm = wecom_module.WeComAlarm()
    alarm.push_status("DEAD [IB_BROKER:7497]", "detail")

    assert len(sent) == 1
    _, payload, _, _ = sent[0]
    assert payload["msgtype"] == "markdown"
    assert "💀 系统状态: DEAD [IB_BROKER:7497]" in payload["markdown"]["content"]


def test_wecom_retry_once_when_api_returns_error(monkeypatch):
    sent = []
    sleep_calls = []
    calls = {"n": 0}

    class _ErrThenOkResponse:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            calls["n"] += 1
            if calls["n"] == 1:
                return {"errcode": 45009, "errmsg": "api freq out of limit"}
            return {"errcode": 0, "errmsg": "ok"}

    def fake_post(url, json=None, headers=None, timeout=0):
        sent.append((url, json, headers, timeout))
        return _ErrThenOkResponse()

    monkeypatch.setattr(wecom_module.config, "WECOM_WEBHOOK", "https://example.invalid/wecom", raising=False)
    monkeypatch.setattr(wecom_module.requests, "post", fake_post)
    monkeypatch.setattr(wecom_module.random, "randint", lambda a, b: 7)
    monkeypatch.setattr(wecom_module.time, "sleep", lambda s: sleep_calls.append(s))

    alarm = wecom_module.WeComAlarm()
    alarm.push_text("retry me")

    assert len(sent) == 2, "API errcode 失败时应重试 1 次。"
    assert sleep_calls == [7.0], "重试前应按随机退避秒数 sleep。"


def test_wecom_retry_once_when_request_raises(monkeypatch):
    sent = []
    sleep_calls = []
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=0):
        calls["n"] += 1
        sent.append((url, json, headers, timeout))
        if calls["n"] == 1:
            raise RuntimeError("timeout")
        return _FakeResponse()

    monkeypatch.setattr(wecom_module.config, "WECOM_WEBHOOK", "https://example.invalid/wecom", raising=False)
    monkeypatch.setattr(wecom_module.requests, "post", fake_post)
    monkeypatch.setattr(wecom_module.random, "randint", lambda a, b: 9)
    monkeypatch.setattr(wecom_module.time, "sleep", lambda s: sleep_calls.append(s))

    alarm = wecom_module.WeComAlarm()
    alarm.push_text("retry on exception")

    assert len(sent) == 2, "网络异常时应重试 1 次。"
    assert sleep_calls == [9.0], "异常后重试前应按随机退避秒数 sleep。"


def test_wecom_push_trade_appends_payoff_summary(monkeypatch):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=0):
        sent.append((url, json, headers, timeout))
        return _FakeResponse()

    monkeypatch.setattr(wecom_module.config, "WECOM_WEBHOOK", "https://example.invalid/wecom", raising=False)
    monkeypatch.setattr(wecom_module.requests, "post", fake_post)

    alarm = wecom_module.WeComAlarm()
    alarm.push_trade({
        "action": "SELL",
        "symbol": "US.MARA261016P9000",
        "price": 0.55,
        "size": 1,
        "value": 55.0,
        "dt": "2026-09-15T10:00:00",
        "payoff_summary": "- 现货参考价：17.50\n- 最大盈利：55.00\n- 最大亏损：845.00\n- 盈亏平衡点：8.45",
    })

    assert len(sent) == 1
    content = sent[0][1]["markdown"]["content"]
    assert "卖出 成交通知" in content
    assert "US.MARA261016P9000" in content
    assert "最大亏损：845.00" in content
    assert "盈亏平衡点：8.45" in content


def test_wecom_push_trade_without_payoff_summary_keeps_stock_layout(monkeypatch):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=0):
        sent.append((url, json, headers, timeout))
        return _FakeResponse()

    monkeypatch.setattr(wecom_module.config, "WECOM_WEBHOOK", "https://example.invalid/wecom", raising=False)
    monkeypatch.setattr(wecom_module.requests, "post", fake_post)

    alarm = wecom_module.WeComAlarm()
    alarm.push_trade({
        "action": "BUY",
        "symbol": "US.MARA",
        "price": 17.5,
        "size": 1,
        "value": 17.5,
        "dt": "2026-09-15T10:00:00",
    })

    content = sent[0][1]["markdown"]["content"]
    assert "买入 成交通知" in content
    assert "最大亏损" not in content
    assert "盈亏平衡点" not in content


def test_wecom_push_trade_combo_title(monkeypatch):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=0):
        sent.append(json)
        return _FakeResponse()

    monkeypatch.setattr(wecom_module.config, "WECOM_WEBHOOK", "https://example.invalid/wecom", raising=False)
    monkeypatch.setattr(wecom_module.requests, "post", fake_post)

    alarm = wecom_module.WeComAlarm()
    alarm.push_trade({
        "action": "COMBO_SELL",
        "symbol": "US.MARA261016P9000",
        "price": 0.35,
        "size": 1,
        "value": 35.0,
        "dt": "2026-09-15T10:00:00",
        "payoff_summary": "- 现货参考价：17.50\n- 最大盈利：35.00\n- 最大亏损：65.00\n- 盈亏平衡点：8.65",
    })

    content = sent[0]["markdown"]["content"]
    assert "🔴 组合 成交通知" in content
    assert "最大亏损：65.00" in content
