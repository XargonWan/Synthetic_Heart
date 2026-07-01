from core import peer_policy


def test_get_peer_ids_and_names_from_synth_peers(monkeypatch):
    rows = [
        {"id": 8243553794, "name": "Aria"},
        {"id": 1122334455, "name": "Sol"},
    ]
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: rows if key == "SYNTH_PEERS" else default,
    )

    assert peer_policy.get_peer_ids() == frozenset({8243553794, 1122334455})
    assert peer_policy.get_peer_names() == {8243553794: "Aria", 1122334455: "Sol"}


def test_get_peer_ids_accepts_json_string(monkeypatch):
    raw = '[{"id": 42, "name": "Nova"}]'
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: raw if key == "SYNTH_PEERS" else default,
    )

    assert peer_policy.get_peer_ids() == frozenset({42})
    assert peer_policy.get_peer_names() == {42: "Nova"}


def test_malformed_synth_peers_fails_open_to_empty(monkeypatch):
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: "not valid json" if key == "SYNTH_PEERS" else default,
    )

    assert peer_policy.get_peer_ids() == frozenset()
    assert peer_policy.get_peer_names() == {}


def test_row_without_name_still_yields_id_but_no_name(monkeypatch):
    rows = [{"id": 7}]
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: rows if key == "SYNTH_PEERS" else default,
    )

    assert peer_policy.get_peer_ids() == frozenset({7})
    assert peer_policy.get_peer_names() == {}


def test_row_with_non_integer_id_is_skipped(monkeypatch):
    rows = [{"id": "not-a-number", "name": "Broken"}, {"id": 5, "name": "Good"}]
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: rows if key == "SYNTH_PEERS" else default,
    )

    assert peer_policy.get_peer_ids() == frozenset({5})
    assert peer_policy.get_peer_names() == {5: "Good"}
