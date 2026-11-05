"""WebUI handlers for retracted claims (called unbound, without the full server)."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from core.soul.repository import InMemorySoulRepository
from core.webui import SynthWebUIInterface


def _server(repo: InMemorySoulRepository) -> Any:
    return SimpleNamespace(
        _soul_plugin=lambda: SimpleNamespace(get_repository=lambda: repo),
        _retraction_to_dict=SynthWebUIInterface._retraction_to_dict,
    )


def _request(body: Any) -> Any:
    async def _json() -> Any:
        return body

    return SimpleNamespace(json=_json)


@pytest.mark.asyncio
async def test_add_list_and_decide() -> None:
    repo = InMemorySoulRepository()
    srv = _server(repo)

    added = await SynthWebUIInterface.post_retraction(
        srv, _request({"subject": "trip to Ca' Senz'Autobus", "reason": "a joke"})
    )
    assert json.loads(added.body)["stored"] == 1

    listed = json.loads((await SynthWebUIInterface.get_retractions(srv)).body)
    (item,) = listed["retractions"]
    assert item["status"] == "active" and item["asserted_by"] == "webui"

    lifted = await SynthWebUIInterface.post_retraction(
        srv, _request({"id": item["id"], "action": "lift"})
    )
    assert json.loads(lifted.body)["retraction"]["status"] == "lifted"


@pytest.mark.asyncio
async def test_bad_requests_are_rejected() -> None:
    srv = _server(InMemorySoulRepository())
    for body, code in (
        ("nope", 400),
        ({"claim": "no subject"}, 400),
        ({"id": "rtr-x", "action": "lift"}, 404),
    ):
        with pytest.raises(HTTPException) as exc:
            await SynthWebUIInterface.post_retraction(srv, _request(body))
        assert exc.value.status_code == code
