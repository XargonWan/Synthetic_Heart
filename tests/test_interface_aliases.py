"""Readable @username aliases for the numeric ids in interface paths.

The numeric id stays canonical (stored, compared, routed); the model is shown and
asked to build ``telegram/@jay`` and its answer is mapped back before anything
routes it. The database is made unreachable so only the in-memory cache and the
conversion logic are exercised.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator

import pytest

from core import interface_aliases as ia

JAY = "31321637"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import core.db as db

    @asynccontextmanager
    async def broken_conn() -> AsyncIterator[None]:
        raise RuntimeError("database offline (test)")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "get_conn_ctx", broken_conn)
    monkeypatch.setattr(ia, "_table_ready", False)
    for cache in (ia._by_alias, ia._by_target, ia._misses):
        cache.clear()
    yield
    for cache in (ia._by_alias, ia._by_target, ia._misses):
        cache.clear()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_a_known_alias_is_shown_instead_of_the_numeric_id() -> None:
    run(ia.remember_alias("telegram", "@JayCheshire", JAY))

    assert run(ia.to_display_path(f"telegram/{JAY}")) == "telegram/@jaycheshire"
    assert run(ia.to_display_path(f"telegram/{JAY}/12")) == "telegram/@jaycheshire/12"


def test_the_alias_maps_back_to_the_stored_id_path() -> None:
    run(ia.remember_alias("telegram", "jaycheshire", JAY))

    assert run(ia.to_canonical_path("telegram/@JayCheshire")) == f"telegram/{JAY}"
    assert run(ia.to_canonical_path("telegram/@jaycheshire/12")) == f"telegram/{JAY}/12"


def test_paths_without_a_known_alias_are_left_alone() -> None:
    assert run(ia.to_display_path(f"telegram/{JAY}")) == f"telegram/{JAY}"
    assert run(ia.to_canonical_path("telegram/@nobodyknown")) == "telegram/@nobodyknown"
    assert run(ia.to_canonical_path(f"telegram/{JAY}")) == f"telegram/{JAY}"
    assert run(ia.to_display_path("synth_webui/webui_default")) == (
        "synth_webui/webui_default"
    )
    assert run(ia.to_display_path(None)) is None


def test_a_renamed_account_replaces_its_old_alias() -> None:
    run(ia.remember_alias("telegram", "oldhandle", JAY))
    run(ia.remember_alias("telegram", "newhandle", JAY))

    assert run(ia.to_display_path(f"telegram/{JAY}")) == "telegram/@newhandle"
    assert run(ia.to_canonical_path("telegram/@oldhandle")) == "telegram/@oldhandle"
    assert run(ia.to_canonical_path("telegram/@newhandle")) == f"telegram/{JAY}"


def test_a_handle_that_moves_to_another_account_follows_it() -> None:
    run(ia.remember_alias("telegram", "shared_handle", "111"))
    run(ia.remember_alias("telegram", "shared_handle", "222"))

    assert run(ia.to_canonical_path("telegram/@shared_handle")) == "telegram/222"
    assert run(ia.to_display_path("telegram/111")) == "telegram/111"


def test_interfaces_do_not_share_aliases() -> None:
    run(ia.remember_alias("telegram", "jaycheshire", JAY))

    assert run(ia.to_display_path(f"telegram_bot/{JAY}")) == f"telegram_bot/{JAY}"
    assert run(ia.to_canonical_path("telegram_bot/@jaycheshire")) == (
        "telegram_bot/@jaycheshire"
    )


def test_a_group_id_can_have_a_public_alias() -> None:
    run(ia.remember_alias("telegram_bot", "tardis_lab", "-1003098886330"))

    assert (
        run(ia.to_display_path("telegram_bot/-1003098886330/2"))
        == "telegram_bot/@tardis_lab/2"
    )
    assert (
        run(ia.to_canonical_path("telegram_bot/@tardis_lab/2"))
        == "telegram_bot/-1003098886330/2"
    )


@pytest.mark.parametrize("bad", ["", "ab", "9lives", "has space", "x" * 40, None])
def test_things_that_are_not_handles_are_ignored(bad: Any) -> None:
    run(ia.remember_alias("telegram", bad, JAY))
    assert run(ia.to_display_path(f"telegram/{JAY}")) == f"telegram/{JAY}"


# -- learning from incoming messages -----------------------------------------


def _message(
    chat_type: Any, chat_id: int, *, sender: str | None, chat_name: str | None
):
    return SimpleNamespace(
        chat_id=chat_id,
        chat=SimpleNamespace(id=chat_id, type=chat_type, username=chat_name),
        from_user=SimpleNamespace(username=sender),
    )


def test_a_private_message_names_the_conversation_after_the_sender() -> None:
    run(
        ia.remember_message_alias(
            "telegram_bot",
            _message("private", int(JAY), sender="JayCheshire", chat_name=None),
        )
    )
    assert run(ia.to_display_path(f"telegram_bot/{JAY}")) == "telegram_bot/@jaycheshire"


def test_an_enum_like_chat_type_is_understood() -> None:
    private = SimpleNamespace(value="private")
    run(
        ia.remember_message_alias(
            "telegram",
            _message(private, int(JAY), sender="jaycheshire", chat_name=None),
        )
    )
    assert run(ia.to_display_path(f"telegram/{JAY}")) == "telegram/@jaycheshire"


def test_a_group_message_uses_the_groups_own_username_not_the_senders() -> None:
    run(
        ia.remember_message_alias(
            "telegram_bot",
            _message(
                "supergroup",
                -1003098886330,
                sender="jaycheshire",
                chat_name="tardis_lab",
            ),
        )
    )
    assert run(ia.to_display_path("telegram_bot/-1003098886330")) == (
        "telegram_bot/@tardis_lab"
    )
    assert run(ia.to_canonical_path("telegram_bot/@jaycheshire")) == (
        "telegram_bot/@jaycheshire"
    )


def test_a_private_group_or_a_sender_without_username_learns_nothing() -> None:
    run(
        ia.remember_message_alias(
            "telegram_bot",
            _message("supergroup", -100123, sender="jaycheshire", chat_name=None),
        )
    )
    run(
        ia.remember_message_alias(
            "telegram_bot", _message("private", 555, sender=None, chat_name=None)
        )
    )
    assert ia._by_alias == {}


# -- the model's answer is mapped back before routing -------------------------


def test_actions_built_with_an_alias_path_are_routed_on_the_stored_path() -> None:
    from core.message_chain import _canonicalize_alias_paths

    run(ia.remember_alias("telegram", "jaycheshire", JAY))
    actions = [
        {
            "type": "send_message",
            "payload": {"text": "ciao", "interface_path": "telegram/@jaycheshire"},
        },
        {"type": "send_message", "payload": {"text": "yo"}},
        {"type": "update_emotion_state", "payload": {"emotions": {}}},
    ]

    out = run(_canonicalize_alias_paths(actions))

    assert out[0]["payload"]["interface_path"] == f"telegram/{JAY}"
    assert "interface_path" not in out[1]["payload"]
