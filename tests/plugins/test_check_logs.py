from plugins.check_logs import CheckLogsPlugin, _tail_lines


class FakeBot:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class FakeMessage:
    def __init__(self, chat_id=12345):
        self.chat_id = chat_id


def write_log_file(path, lines=100):
    with open(path, "w") as f:
        for i in range(1, lines + 1):
            f.write(f"2025-01-01 00:00:00 - line {i}\n")


def test_tail_lines(tmp_path):
    p = tmp_path / "synth.log"
    write_log_file(str(p), lines=50)
    last_5 = _tail_lines(str(p), 5)
    assert len(last_5) == 5
    assert "line 50" in last_5[-1]


def test_check_logs_plugin(monkeypatch, tmp_path):
    # point log dir to tmp_path
    monkeypatch.setenv("SYNTH_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "synth.log"
    write_log_file(str(log_file), lines=80)

    plugin = CheckLogsPlugin()
    bot = FakeBot()
    msg = FakeMessage()

    action = {"type": "get_logs", "payload": {"file": "synth.log", "lines": 10}}
    plugin.execute_action(action, {}, bot, msg)

    assert len(bot.messages) == 1
    chat_id, text = bot.messages[0]
    assert chat_id == msg.chat_id
    assert "line 80" in text
    assert "line 71" in text


def test_search_logs_keyword(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTH_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "synth.log"
    with open(log_file, "w") as f:
        f.write("Error: something failed\n")
        f.write("Info: all good\n")
        f.write("Exception: crash at module\n")

    plugin = CheckLogsPlugin()
    bot = FakeBot()
    msg = FakeMessage()

    action = {
        "type": "search_logs",
        "payload": {
            "file": "synth.log",
            "queries": ["error", "exception"],
            "regex": False,
        },
    }
    plugin.execute_action(action, {}, bot, msg)

    assert len(bot.messages) == 1
    _, text = bot.messages[0]
    assert "Error: something failed" in text
    assert "Exception: crash" in text


def test_search_logs_regex(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTH_LOG_DIR", str(tmp_path))
    log_file = tmp_path / "synth.log"
    with open(log_file, "w") as f:
        f.write("2025-01-01 ERROR code=500\n")
        f.write("2025-01-01 INFO code=200\n")

    plugin = CheckLogsPlugin()
    bot = FakeBot()
    msg = FakeMessage()

    action = {
        "type": "search_logs",
        "payload": {"file": "synth.log", "queries": ["code=5\\d\\d"], "regex": True},
    }
    plugin.execute_action(action, {}, bot, msg)

    assert len(bot.messages) == 1
    _, text = bot.messages[0]
    assert "code=500" in text
