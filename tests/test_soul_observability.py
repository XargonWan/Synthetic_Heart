import importlib

import core.soul.observability as soul_observability


def test_maybe_langfuse_trace_logs_warning_once_on_init_failure(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    module = importlib.reload(soul_observability)
    warnings = []

    class DummyLogger:
        def warning(self, message: str, *args, **kwargs) -> None:
            if args:
                message = message % args
            warnings.append(message)

    def _raise_import_error(_name: str):
        raise RuntimeError("init boom")

    monkeypatch.setattr(module, "_get_runtime_logger", lambda: DummyLogger())
    monkeypatch.setattr(module, "import_module", _raise_import_error)
    module._langfuse_warning_keys.clear()

    with module.maybe_langfuse_trace("post_session_compile") as trace:
        assert trace is None
    with module.maybe_langfuse_trace("post_session_compile") as trace:
        assert trace is None

    assert len(warnings) == 1
    assert "Failed to initialize SOUL Langfuse trace" in warnings[0]


def test_maybe_langfuse_trace_flushes_client_when_trace_has_no_flush(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    module = importlib.reload(soul_observability)
    warnings = []
    flush_calls = []
    trace_updates = []

    class DummyLogger:
        def warning(self, message: str, *args, **kwargs) -> None:
            if args:
                message = message % args
            warnings.append(message)

    class DummyTrace:
        def update(self, **kwargs) -> None:
            trace_updates.append(kwargs)

    class DummyClient:
        def trace(self, **kwargs):
            trace_updates.append(kwargs)
            return DummyTrace()

        def flush(self) -> None:
            flush_calls.append("flush")

    monkeypatch.setattr(module, "_get_runtime_logger", lambda: DummyLogger())
    monkeypatch.setattr(
        module,
        "import_module",
        lambda _name: type("LangfuseModule", (), {"Langfuse": DummyClient}),
    )
    module._langfuse_warning_keys.clear()

    with module.maybe_langfuse_trace("nightly_rollup") as trace:
        assert trace is not None
        trace.update(output={"ok": True})

    assert flush_calls == ["flush"]
    assert warnings == []
