import alarms.dingtalk_alarm as dingtalk_module


class _FakeResponse:
    status_code = 200
    text = "ok"

    @staticmethod
    def json():
        return {"errcode": 0, "errmsg": "ok"}


def test_dingtalk_push_text_sends_payload(monkeypatch):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=0):
        sent.append((url, json, headers, timeout))
        return _FakeResponse()

    monkeypatch.setattr(dingtalk_module.config, "DINGTALK_WEBHOOK", "https://example.invalid/dingtalk")
    monkeypatch.setattr(dingtalk_module.config, "DINGTALK_SECRET", "", raising=False)
    monkeypatch.setattr(dingtalk_module.requests, "post", fake_post)

    alarm = dingtalk_module.DingTalkAlarm()
    alarm.push_text("hello")

    assert len(sent) == 1
    _, payload, _, _ = sent[0]
    assert payload["msgtype"] == "text"
    assert "hello" in payload["text"]["content"]


def test_dingtalk_retry_once_with_random_sleep_when_api_error(monkeypatch):
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
                return {"errcode": 123, "errmsg": "rate limited"}
            return {"errcode": 0, "errmsg": "ok"}

    def fake_post(url, json=None, headers=None, timeout=0):
        sent.append((url, json, headers, timeout))
        return _ErrThenOkResponse()

    monkeypatch.setattr(dingtalk_module.config, "DINGTALK_WEBHOOK", "https://example.invalid/dingtalk")
    monkeypatch.setattr(dingtalk_module.config, "DINGTALK_SECRET", "", raising=False)
    monkeypatch.setattr(dingtalk_module.requests, "post", fake_post)
    monkeypatch.setattr(dingtalk_module.random, "randint", lambda a, b: 6)
    monkeypatch.setattr(dingtalk_module.time, "sleep", lambda s: sleep_calls.append(s))

    alarm = dingtalk_module.DingTalkAlarm()
    alarm.push_text("retry")

    assert len(sent) == 2, "API 失败时应重试 1 次。"
    assert sleep_calls == [6.0], "重试前应使用随机退避秒数。"


def test_dingtalk_retry_once_with_random_sleep_when_request_raises(monkeypatch):
    sent = []
    sleep_calls = []
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=0):
        calls["n"] += 1
        sent.append((url, json, headers, timeout))
        if calls["n"] == 1:
            raise RuntimeError("timeout")
        return _FakeResponse()

    monkeypatch.setattr(dingtalk_module.config, "DINGTALK_WEBHOOK", "https://example.invalid/dingtalk")
    monkeypatch.setattr(dingtalk_module.config, "DINGTALK_SECRET", "", raising=False)
    monkeypatch.setattr(dingtalk_module.requests, "post", fake_post)
    monkeypatch.setattr(dingtalk_module.random, "randint", lambda a, b: 10)
    monkeypatch.setattr(dingtalk_module.time, "sleep", lambda s: sleep_calls.append(s))

    alarm = dingtalk_module.DingTalkAlarm()
    alarm.push_text("retry exception")

    assert len(sent) == 2, "异常时应重试 1 次。"
    assert sleep_calls == [10.0], "异常重试前应使用随机退避秒数。"


def test_dingtalk_push_status_with_context_keeps_lifecycle_icon(monkeypatch):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=0):
        sent.append((url, json, headers, timeout))
        return _FakeResponse()

    monkeypatch.setattr(dingtalk_module.config, "DINGTALK_WEBHOOK", "https://example.invalid/dingtalk")
    monkeypatch.setattr(dingtalk_module.config, "DINGTALK_SECRET", "", raising=False)
    monkeypatch.setattr(dingtalk_module.requests, "post", fake_post)

    alarm = dingtalk_module.DingTalkAlarm()
    alarm.push_status("DEAD [IB_BROKER:7497]", "detail")
    alarm.push_status("ALIVE [IB_BROKER:7497]", "detail")

    assert len(sent) == 2
    _, dead_payload, _, _ = sent[0]
    _, alive_payload, _, _ = sent[1]
    assert dead_payload["msgtype"] == "markdown"
    assert "💀 系统状态: DEAD [IB_BROKER:7497]" in dead_payload["markdown"]["text"]
    assert "✅ 系统状态: ALIVE [IB_BROKER:7497]" in alive_payload["markdown"]["text"]
