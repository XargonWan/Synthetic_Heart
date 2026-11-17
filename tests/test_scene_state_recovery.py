"""Scene state/store recovery: corruption, shape validation, evidence strength.

Independent of conftest; real SQLite files live under pytest tmp_path.
"""

import importlib
import sqlite3

import pytest

NOW = 1000.0


def _entry(value, *, updated_at=NOW - 10, source="human"):
    return {"value": value, "updated_at": updated_at, "source": source}


def _human_location_patch(value, evidence, source_text):
    return {"changes": [{"field": "location", "value": value, "evidence": evidence}]}


# --- (1) SceneStore corruption recovery -------------------------------------


@pytest.mark.asyncio
async def test_malformed_fields_json_loads_empty_and_save_overwrites(tmp_path):
    from plugins.scene_context.store import SceneStore

    store = SceneStore(tmp_path / "scenes.sqlite3")
    await store.load("telegram_bot/1")
    assert await store.save(
        "telegram_bot/1",
        {"location": _entry("bed", updated_at=100)},
        expected_revision=0,
    )
    with sqlite3.connect(store.path) as raw:
        raw.execute(
            "UPDATE scene_context SET fields_json = ? WHERE interface_path = ?",
            ("{not json", "telegram_bot/1"),
        )
    loaded = await store.load("telegram_bot/1")
    assert loaded == {"revision": 1, "fields": {}}

    replacement = {"activity": _entry("reading", updated_at=200)}
    assert await store.save(
        "telegram_bot/1", replacement, expected_revision=loaded["revision"]
    )
    reloaded = await SceneStore(store.path).load("telegram_bot/1")
    assert reloaded == {"revision": 2, "fields": replacement}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ['["bed"]', '"bed"', "null", "42", "true"])
async def test_non_dict_fields_json_loads_as_empty(tmp_path, payload):
    from plugins.scene_context.store import SceneStore

    store = SceneStore(tmp_path / "scenes.sqlite3")
    await store.load("telegram_bot/1")
    with sqlite3.connect(store.path) as raw:
        raw.execute(
            "INSERT INTO scene_context (interface_path, revision, fields_json) "
            "VALUES (?, ?, ?)",
            ("telegram_bot/1", 7, payload),
        )
    loaded = await store.load("telegram_bot/1")
    assert loaded == {"revision": 7, "fields": {}}
    assert await store.save("telegram_bot/1", {}, expected_revision=7)
    assert (await store.load("telegram_bot/1"))["revision"] == 8


@pytest.mark.asyncio
async def test_corrupt_scope_does_not_touch_other_scopes(tmp_path):
    from plugins.scene_context.store import SceneStore

    store = SceneStore(tmp_path / "scenes.sqlite3")
    good = {"location": _entry("bed", updated_at=100)}
    await store.load("telegram_bot/2")
    assert await store.save("telegram_bot/2", good, expected_revision=0)
    await store.load("telegram_bot/1")
    with sqlite3.connect(store.path) as raw:
        raw.execute(
            "INSERT INTO scene_context (interface_path, revision, fields_json) "
            "VALUES (?, ?, ?)",
            ("telegram_bot/1", 1, "{broken"),
        )
    assert (await store.load("telegram_bot/1"))["fields"] == {}
    assert (await store.load("telegram_bot/2"))["fields"] == good


@pytest.mark.asyncio
async def test_store_closes_every_connection(tmp_path, monkeypatch):
    from plugins.scene_context import store as store_module

    opened = []
    real_connect = store_module.sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(store_module.sqlite3, "connect", tracking_connect)
    store = store_module.SceneStore(tmp_path / "scenes.sqlite3")
    await store.load("telegram_bot/1")
    assert await store.save("telegram_bot/1", {}, expected_revision=0)
    assert not await store.save("telegram_bot/1", {}, expected_revision=0)
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


# --- (2) active_fields shape validation -------------------------------------


@pytest.mark.parametrize(
    "bad_entry",
    [
        "bed",
        None,
        {"value": "bed", "source": "human"},
        {"value": "bed", "updated_at": True, "source": "human"},
        {"value": "bed", "updated_at": float("nan"), "source": "human"},
        {"value": "bed", "updated_at": float("inf"), "source": "human"},
        {"value": "bed", "updated_at": "990", "source": "human"},
        {"value": "bed", "updated_at": NOW - 10},
        {"value": "bed", "updated_at": NOW - 10, "source": "robot"},
        {"value": 12, "updated_at": NOW - 10, "source": "human"},
        {"value": "", "updated_at": NOW - 10, "source": "human"},
        {"value": "   ", "updated_at": NOW - 10, "source": "human"},
        {"value": "x" * 241, "updated_at": NOW - 10, "source": "human"},
        {"value": "x" * 121, "updated_at": NOW - 10, "source": "assistant"},
    ],
)
def test_invalid_scalar_entries_are_dropped_individually(bad_entry):
    from plugins.scene_context.state import active_fields

    scene = {
        "location": bad_entry,
        "activity": _entry("reading", updated_at=NOW - 5),
    }
    assert active_fields(scene, now=NOW) == {"activity": scene["activity"]}


def test_valid_length_boundaries_survive_by_source():
    from plugins.scene_context.state import active_fields

    human_long = _entry("x" * 240)
    assistant_max = _entry("y" * 120, source="assistant")
    scene = {"activity": human_long, "self.activity": assistant_max}
    assert active_fields(scene, now=NOW) == scene


@pytest.mark.parametrize(
    "bad_value",
    [
        "on top",
        None,
        ["self", "on top of", "human"],
        {"subject": "self", "predicate": "on top of"},
        {"subject": "self", "predicate": "on top of", "object": "robot"},
        {"subject": "self", "predicate": "on top of", "object": "self"},
        {"subject": "human", "predicate": "on top of", "object": "human"},
        {"subject": "self", "predicate": 5, "object": "human"},
        {"subject": "self", "predicate": "", "object": "human"},
        {"subject": "self", "predicate": "x" * 81, "object": "human"},
    ],
)
def test_malformed_relation_is_dropped_and_render_survives(bad_value):
    from plugins.scene_context.state import active_fields, render_scene

    scene = {
        "location": _entry("bed", updated_at=NOW - 10),
        "relation": {"value": bad_value, "updated_at": NOW - 10, "source": "human"},
    }
    assert set(active_fields(scene, now=NOW)) == {"location"}
    rendered = render_scene(scene, now=NOW)
    assert rendered == "Shared location: bed"


def test_relation_predicate_is_whitespace_normalised_and_kept():
    from plugins.scene_context.state import active_fields, render_scene

    scene = {
        "relation": {
            "value": {
                "subject": "self",
                "predicate": "  on   top\tof ",
                "object": "human",
            },
            "updated_at": NOW - 10,
            "source": "human",
        }
    }
    rendered = render_scene(scene, now=NOW)
    assert rendered == "Relation: You (Synth) on top of Human"
    assert (
        active_fields(scene, now=NOW)["relation"]["value"]["predicate"] == "on top of"
    )


@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), True, None])
def test_non_finite_or_bool_updated_at_is_rejected_and_renders_safely(bad_time):
    from plugins.scene_context.state import active_fields, render_scene

    scene = {
        "location": {"value": "bed", "updated_at": bad_time, "source": "human"},
        "activity": _entry("reading", updated_at=NOW - 1),
    }
    assert set(active_fields(scene, now=NOW)) == {"activity"}
    assert render_scene(scene, now=NOW) == "Activity: reading"


def test_apply_patch_never_throws_on_malformed_scene_entries():
    from plugins.scene_context.state import apply_patch

    scene = {
        "location": "not-an-entry",
        "relation": {"value": ["bad"], "updated_at": float("nan"), "source": "human"},
        "activity": _entry("reading", updated_at=NOW - 1),
    }
    result = apply_patch(
        scene,
        {
            "changes": [
                {
                    "field": "self.activity",
                    "value": "lying down",
                    "evidence": "lie down",
                }
            ]
        },
        source_text="I lie down",
        phase="assistant",
        now=NOW,
    )
    assert set(result) == {"activity", "self.activity"}
    assert result["self.activity"]["value"] == "lying down"


# --- (3) evidence strength ---------------------------------------------------


@pytest.mark.parametrize(
    ("source", "evidence", "value"),
    [
        ("bed", "bed", "bed"),
        ("in bed", "in bed", "bed"),
        ("phone", "phone", "holding phone"),
    ],
)
def test_short_exact_source_message_is_accepted(source, evidence, value):
    from plugins.scene_context.state import apply_patch

    result = apply_patch(
        {},
        {"changes": [{"field": "location", "value": value, "evidence": evidence}]},
        source_text=source,
        phase="human",
        now=NOW,
    )
    assert result["location"]["value"] == value


@pytest.mark.parametrize(
    ("source", "evidence"),
    [
        ("I am in bed", "in"),
        ("hold on", "on"),
        ("I am in bed", "a"),
        ("a", "a"),
        ("on", "on"),
        ("the bedroom is quiet", "bedroom"),
        ("xcuddling together", "cuddling together"),
        ("I am in bed", "bed"),
    ],
)
def test_weak_or_partial_word_evidence_is_rejected(source, evidence):
    from plugins.scene_context.state import apply_patch

    assert (
        apply_patch(
            {},
            {"changes": [{"field": "location", "value": "bed", "evidence": evidence}]},
            source_text=source,
            phase="human",
            now=NOW,
        )
        == {}
    )


def test_long_exact_quote_inside_full_message_is_accepted_at_word_boundaries():
    from plugins.scene_context.state import apply_patch

    result = apply_patch(
        {},
        {
            "changes": [
                {
                    "field": "activity",
                    "value": "cuddling",
                    "evidence": "we are cuddling",
                }
            ]
        },
        source_text="ok, we are cuddling now.",
        phase="human",
        now=NOW,
    )
    assert result["activity"]["value"] == "cuddling"


@pytest.mark.parametrize("reset_evidence", ["a", "on", " a ", "in"])
def test_weak_reset_evidence_cannot_clear_scene(reset_evidence):
    from plugins.scene_context.state import apply_patch

    old = {"location": _entry("bed", updated_at=100)}
    result = apply_patch(
        old,
        {"reset": True, "reset_evidence": reset_evidence, "changes": []},
        source_text="a on in",
        phase="human",
        now=110,
    )
    assert result == old


def test_short_exact_reset_message_clears_scene():
    from plugins.scene_context.state import apply_patch

    old = {"location": _entry("bed", updated_at=100)}
    result = apply_patch(
        old,
        {"reset": True, "reset_evidence": "reset", "changes": []},
        source_text="reset",
        phase="human",
        now=110,
    )
    assert result == {}


# --- length bounds on the patch path ----------------------------------------


@pytest.mark.parametrize(
    ("phase", "field", "length", "accepted"),
    [
        ("assistant", "self.activity", 120, True),
        ("assistant", "self.activity", 121, False),
        ("human", "location", 240, True),
        ("human", "location", 241, False),
    ],
)
def test_scalar_length_bounds_depend_on_source_phase(phase, field, length, accepted):
    from plugins.scene_context.state import apply_patch

    text = "w" * length
    result = apply_patch(
        {},
        {"changes": [{"field": field, "value": text, "evidence": text}]},
        source_text=text,
        phase=phase,
        now=NOW,
    )
    assert (field in result) is accepted


def test_patch_relation_predicate_bounded_and_normalised():
    from plugins.scene_context.state import apply_patch

    relation = {"subject": "self", "predicate": "x" * 80, "object": "human"}
    ok = apply_patch(
        {},
        {
            "changes": [
                {
                    "field": "relation",
                    "value": relation,
                    "evidence": "you are on top of me",
                }
            ]
        },
        source_text="you are on top of me",
        phase="human",
        now=NOW,
    )
    assert ok["relation"]["value"]["predicate"] == "x" * 80
    too_long = dict(relation, predicate="x" * 81)
    rejected = apply_patch(
        {},
        {
            "changes": [
                {
                    "field": "relation",
                    "value": too_long,
                    "evidence": "you are on top of me",
                }
            ]
        },
        source_text="you are on top of me",
        phase="human",
        now=NOW,
    )
    assert rejected == {}


def test_state_module_imports_cleanly():
    importlib.import_module("plugins.scene_context.state")
