import core.cortex_api_logger as cal


def test_cortex_api_logger_noop_when_all_toggles_off(monkeypatch):
    monkeypatch.setenv("CORTEX_API_LOG_ENABLED", "false")
    monkeypatch.setenv("CORTEX_LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")

    def _boom_logger():
        raise AssertionError("logger should not be initialized when toggles are off")

    monkeypatch.setattr(cal, "_get_logger", _boom_logger)

    cal.log_cortex_request("test", model="m", payload={"x": 1})
    cal.log_cortex_response("test", model="m", status=200, body={"ok": True})


def test_cortex_api_logger_file_toggle_logs_without_langfuse(monkeypatch):
    monkeypatch.setenv("CORTEX_API_LOG_ENABLED", "true")
    monkeypatch.setenv("CORTEX_LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")

    lines = []

    class DummyLogger:
        def debug(self, message: str) -> None:
            lines.append(message)

    monkeypatch.setattr(cal, "_get_logger", lambda: DummyLogger())

    cal.log_cortex_request("test", model="m", payload={"x": 1})
    cal.log_cortex_response("test", model="m", status=200, body={"ok": True})

    assert len(lines) == 2
    assert "REQUEST" in lines[0]
    assert "RESPONSE" in lines[1]
    assert "request_id=" in lines[0]
    assert "request_id=" in lines[1]


def test_langfuse_full_payload_when_redaction_disabled(monkeypatch):
    monkeypatch.setenv("CORTEX_API_LOG_ENABLED", "false")
    monkeypatch.setenv("CORTEX_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("CORTEX_LANGFUSE_REDACT_PAYLOADS", "false")
    monkeypatch.setenv("CORTEX_LANGFUSE_CAPTURE_GENERATIONS", "true")
    monkeypatch.setenv("CORTEX_LANGFUSE_CAPTURE_HEADERS", "true")

    captured = {
        "trace_input": None,
        "generation_input": None,
        "trace_headers": None,
        "generation_headers": None,
    }

    class DummyTrace:
        def update(self, **kwargs):
            if "input" in kwargs:
                captured["trace_input"] = kwargs["input"]
            headers = kwargs.get("metadata", {}).get("headers")
            if headers is not None:
                captured["trace_headers"] = headers

        def generation(self, **kwargs):
            captured["generation_input"] = kwargs.get("input")
            captured["generation_headers"] = kwargs.get("metadata", {}).get("headers")

    class DummyClient:
        def trace(self, **kwargs):
            return DummyTrace()

        def flush(self):
            return None

    monkeypatch.setattr(cal, "_get_langfuse_client", lambda: DummyClient())

    payload = {"prompt": "x" * 2000}
    headers = {"Authorization": "Bearer secret", "X-Debug": "1"}
    cal.log_cortex_request("test", model="m", headers=headers, payload=payload)
    cal.log_cortex_response("test", model="m", status=200, body={"ok": True})

    assert captured["trace_input"] == payload
    assert captured["generation_input"] == payload
    assert captured["trace_headers"] == {"Authorization": "***", "X-Debug": "1"}
    assert captured["generation_headers"] == {"Authorization": "***", "X-Debug": "1"}


def test_pop_langfuse_request_does_not_pop_unrelated_engine() -> None:
    token = cal._langfuse_ctx.set(
        [
            {
                "request_id": "req-openrouter",
                "trace": object(),
                "engine": "openrouter",
                "model": "m1",
                "url": "",
                "headers": None,
                "input_payload": {},
                "started_at_monotonic": 1.0,
            },
            {
                "request_id": "req-gemini",
                "trace": object(),
                "engine": "gemini:gemini",
                "model": "m2",
                "url": "",
                "headers": None,
                "input_payload": {},
                "started_at_monotonic": 2.0,
            },
        ]
    )
    try:
        popped = cal._pop_langfuse_request(engine="gemini:gemini", model="wrong-model")
        assert popped is not None
        assert popped["request_id"] == "req-gemini"

        remaining = cal._langfuse_ctx.get()
        assert len(remaining) == 1
        assert remaining[0]["request_id"] == "req-openrouter"

        unmatched = cal._pop_langfuse_request(engine="anthropic", model="x")
        assert unmatched is None
        assert cal._langfuse_ctx.get()[0]["request_id"] == "req-openrouter"
    finally:
        cal._langfuse_ctx.reset(token)


def test_langfuse_uses_placeholder_output_for_empty_body(monkeypatch):
    monkeypatch.setenv("CORTEX_API_LOG_ENABLED", "false")
    monkeypatch.setenv("CORTEX_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("CORTEX_LANGFUSE_CAPTURE_GENERATIONS", "true")

    captured = {"trace_output": None, "generation_output": None}

    class DummyTrace:
        def update(self, **kwargs):
            if "output" in kwargs:
                captured["trace_output"] = kwargs["output"]

        def generation(self, **kwargs):
            captured["generation_output"] = kwargs.get("output")

    class DummyClient:
        def trace(self, **kwargs):
            return DummyTrace()

        def flush(self):
            return None

    monkeypatch.setattr(cal, "_get_langfuse_client", lambda: DummyClient())

    cal.log_cortex_request("test", model="m", payload={"x": 1})
    cal.log_cortex_response("test", model="m", status=200, body=None)

    expected = {"empty_response": True, "reason": "body_none"}
    assert captured["trace_output"] == expected
    assert captured["generation_output"] == expected
