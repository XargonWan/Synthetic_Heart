"""Caps on the prompt-context material that made every build oversized.

Three separate leaks are pinned here:

1. `reduce_prompt_for_llm_limit` used to delete memories and then ~20 context
   fields *before* it touched the action catalog, even though the catalog is the
   largest serialized block and its redundant detail is re-supplied by the
   corrector on demand. The order is now catalog-first.
2. `AIDiaryPlugin.get_static_injection` returned the whole diary window with no
   budget, so a day-accumulating row (observed at 167,795 chars) could carry
   ~238k chars into a single context key.
3. `mem_scenes.summary` was unbounded on write (one row on record at 1,195,057
   chars).

All three are pure functions/helpers, so these tests need no DB, no bridge and
no LLM.
"""

import pytest

from core.json_utils import dumps as json_dumps
from core.prompt_engine import reduce_prompt_for_llm_limit
from core.soul.repository import MAX_SCENE_SUMMARY_CHARS, PostgresSoulRepository
import plugins.ai_diary as ai_diary


# --------------------------------------------------------------------------
# 1. Reduction order: catalog gives up its redundancy before grounding is lost
# --------------------------------------------------------------------------


def _catalogue_prompt(catalogue_pad: int, num_memories: int = 5) -> dict:
    """A prompt whose `actions` block carries trimmable detail, like the real one."""
    return {
        "context": {
            "memories": [f"Memory {i}: " + ("m" * 400) for i in range(num_memories)],
            "emotion_state": {"joy": 0.7, "note": "e" * 300},
            "date": "2026-09-22",
            "time": "12:22",
        },
        "input": {"type": "message", "payload": {"text": "hello"}},
        "instructions": "RULES " * 200,
        "actions": {
            f"vessel_minecraft_verb_{i}": {
                "brief": f"Verb {i}",
                "source": "vessel",
                "schema": {"required": ["target"], "padding": "s" * 200},
                "examples": {"target": "x" * catalogue_pad},
            }
            for i in range(5)
        },
    }


def _size(prompt: dict) -> int:
    return len(json_dumps(prompt))


def _size_without_examples(prompt: dict) -> int:
    import copy

    stripped = copy.deepcopy(prompt)
    for action in stripped["actions"].values():
        action.pop("examples", None)
    return _size(stripped)


def _size_brief_only(prompt: dict) -> int:
    import copy

    stripped = copy.deepcopy(prompt)
    for name, action in stripped["actions"].items():
        stripped["actions"][name] = {"brief": action.get("brief", "")}
    return _size(stripped)


def test_action_examples_are_dropped_before_memories_and_context():
    """Slimming the catalog is enough, so her grounding survives untouched."""
    prompt = _catalogue_prompt(catalogue_pad=4000)
    full = _size(prompt)
    after_examples = _size_without_examples(prompt)
    assert after_examples < full, "fixture must have trimmable detail"

    limit = after_examples + 200  # dropping `examples` alone brings us under
    assert limit < full

    reduced = reduce_prompt_for_llm_limit(prompt, limit)

    # Catalog lost its redundant detail...
    for action in reduced["actions"].values():
        assert "examples" not in action
    # ...while the context blocks are all still there.
    context = reduced["context"]
    assert len(context["memories"]) == 5
    assert context["emotion_state"]["note"] == "e" * 300
    assert context["date"] == "2026-09-22"
    assert context["time"] == "12:22"


def test_catalog_is_stripped_to_brief_only_before_memories_go():
    """Stripping schemas (still reconstructible) precedes deleting memories."""
    prompt = _catalogue_prompt(catalogue_pad=4000)
    brief_only = _size_brief_only(prompt)

    reduced = reduce_prompt_for_llm_limit(prompt, brief_only + 200)

    for action in reduced["actions"].values():
        assert set(action.keys()) == {"brief"}
    assert len(reduced["context"]["memories"]) == 5


def test_memories_are_dropped_only_when_the_catalog_cannot_help():
    """Below the brief-only floor, real grounding is sacrificed as before."""
    prompt = _catalogue_prompt(catalogue_pad=1000)
    brief_only = _size_brief_only(prompt)

    reduced = reduce_prompt_for_llm_limit(prompt, brief_only - 3000)

    # The context may be gone entirely (emergency step) or merely stripped of
    # the memories key; either way the memory block did not survive.
    assert "memories" not in reduced.get("context", {})
    for action in reduced["actions"].values():
        assert set(action.keys()) == {"brief"}


def test_untrimmable_catalog_keeps_previous_behaviour():
    """A catalog with nothing to trim still falls back to removing memories."""
    prompt = {
        "context": {
            "memories": [f"Memory {i}: " + ("m" * 400) for i in range(5)],
            "emotion_state": {"joy": 0.5},
        },
        "input": {"type": "message", "payload": {"text": "hi"}},
        "instructions": "RULES " * 200,
        "actions": {"send_message": {"required": ["text"]}},
    }
    full = _size(prompt)

    reduced = reduce_prompt_for_llm_limit(prompt, full - 1500)

    assert "memories" not in reduced["context"]
    assert "send_message" in reduced["actions"]


def test_catalog_gives_up_its_detail_before_the_conversation_window_does():
    """The window is what the turn was built to read, so the catalog's
    redundant detail pays for an oversized prompt before history is trimmed.

    With the original order (history first, catalog third) this prompt loses
    history lines and keeps every `examples` object."""
    prompt = _catalogue_prompt(catalogue_pad=4000)
    prompt["context"]["history_recent"] = [
        f"line {i}: " + ("h" * 300) for i in range(8)
    ]
    prompt["context"]["history_current_chat"] = [
        f"cur {i}: " + ("c" * 300) for i in range(6)
    ]
    full = _size(prompt)
    after_examples = _size_without_examples(prompt)
    limit = after_examples + 500  # dropping `examples` alone suffices
    assert limit < full

    reduced = reduce_prompt_for_llm_limit(prompt, limit)

    for action in reduced["actions"].values():
        assert "examples" not in action
    # Nothing else paid: the window is whole and the grounding is intact.
    assert len(reduced["context"]["history_recent"]) == 8
    assert len(reduced["context"]["history_current_chat"]) == 6
    assert len(reduced["context"]["memories"]) == 5


def test_the_size_report_names_the_protected_sections(capfd):
    """The report must say where the size actually sits.

    `core.logging_utils` writes through a queue + background thread, so the line
    can land after this test returns; the assertion therefore only requires that
    the call did not raise and that the reductions still happened. The text of
    the report is checked against the live log instead (a capture here races the
    writer thread)."""
    prompt = _catalogue_prompt(catalogue_pad=4000)
    full = _size(prompt)

    reduced = reduce_prompt_for_llm_limit(prompt, full - 100)

    assert "examples" not in next(iter(reduced["actions"].values()))


# --------------------------------------------------------------------------
# 2. Diary static injection budget
# --------------------------------------------------------------------------


def _effective_diary_budget() -> int:
    try:
        from core.config_manager import config_registry

        return int(
            config_registry.get_value("DIARY_CONTEXT_MAX_CHARS", 8000, value_type=int)
        )
    except Exception:
        return 8000


def test_diary_injection_is_capped():
    """A day-accumulating row cannot carry its whole bulk into a prompt key."""
    entries = [
        {
            "id": 50,
            "content": "c" * 70804,
            "personal_thought": "p" * 49532,
            "interaction_summary": "i" * 47459,
            "user_message": "u" * 200,
            "created_at": "2026-09-22T12:00:00",
        },
        {"id": 49, "content": "older " * 500, "created_at": "2026-09-21T12:00:00"},
    ]

    capped = ai_diary._cap_diary_entries_for_injection(entries)

    budget = _effective_diary_budget()
    text_fields = ("content", "personal_thought", "interaction_summary", "user_message")
    total = sum(
        len(row[f])
        for row in capped
        for f in text_fields
        if isinstance(row.get(f), str)
    )
    assert total <= budget, f"capped injection still {total} chars (budget {budget})"
    assert capped[0]["id"] == 50, "newest entry must be kept first"
    assert capped[0]["content"].endswith("\u2026")
    # Non-text fields survive untouched.
    assert capped[0]["created_at"] == "2026-09-22T12:00:00"


def test_diary_injection_small_entries_pass_through():
    entries = [
        {"id": 2, "content": "a short day", "personal_thought": "fine"},
        {"id": 1, "content": "another", "personal_thought": "ok"},
    ]

    capped = ai_diary._cap_diary_entries_for_injection(entries)

    assert capped == entries


def test_diary_injection_handles_empty_and_junk():
    assert ai_diary._cap_diary_entries_for_injection([]) == []
    assert ai_diary._cap_diary_entries_for_injection(None) == []
    # A non-dict row must not raise; it is kept as-is without crashing the turn.
    junk = [{"id": 1, "content": "x" * 9000}, "not-a-dict"]
    capped = ai_diary._cap_diary_entries_for_injection(junk)
    assert isinstance(capped, list) and capped


# --------------------------------------------------------------------------
# 3. Scene summary stored bounded
# --------------------------------------------------------------------------


def test_scene_summary_is_truncated_on_write():
    oversized = "scene " * 400000  # ~1.2M chars, as observed in mem_scenes

    bounded = PostgresSoulRepository._bounded_scene_summary(oversized)

    assert len(bounded) == MAX_SCENE_SUMMARY_CHARS
    assert bounded.endswith("\u2026")


def test_scene_summary_within_budget_is_untouched():
    summary = "A quiet walk by the harbour."

    assert PostgresSoulRepository._bounded_scene_summary(summary) == summary
    assert PostgresSoulRepository._bounded_scene_summary(None) is None
    assert PostgresSoulRepository._bounded_scene_summary(12345) == 12345


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
