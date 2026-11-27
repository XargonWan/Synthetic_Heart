"""One expandable card per turn: stages fill it in, the final status is processed or failed."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from core import turn_cards as tc


@pytest.fixture(autouse=True)
def fresh() -> None:
    tc.reset_for_tests()
    yield
    tc.reset_for_tests()


def begin(**kw):
    return tc.begin(
        kind="chat", interface="synth_webui", interface_path="synth_webui/s1", **kw
    )


class TestAttempts:
    def test_the_correct_answer_wins_and_the_wrong_ones_are_not_kept(self) -> None:
        card = begin(trigger={"text": "hi"})
        card.attempt("garbage {", valid=False)
        card.attempt('{"actions": []}', valid=True)
        card.attempt("more garbage", valid=False)
        assert card.reply == {"text": '{"actions": []}', "valid": True}
        assert card.attempts == 3
        assert "garbage" not in json.dumps(card.to_dict())

    def test_without_a_correct_answer_the_last_one_that_arrived_is_kept(self) -> None:
        card = begin()
        card.attempt("first", valid=False)
        card.attempt("second", valid=False)
        assert card.reply == {"text": "second", "valid": False}

    def test_no_answer_at_all(self) -> None:
        assert begin().reply is None

    def test_a_very_long_answer_is_cut(self) -> None:
        card = begin()
        card.attempt("x" * (tc.MAX_REPLY + 500), valid=True)
        assert len(card.reply["text"]) == tc.MAX_REPLY


class TestFailures:
    """The card says why no answer arrived."""

    def test_a_failed_model_call_is_remembered_with_its_engine_and_reason(self) -> None:
        card = begin()
        tc.note_failure("llm", "ConnectError: Connection error.", "zen-llm-engine")
        (failure,) = card.to_dict()["failures"]
        assert failure == {
            "stage": "llm",
            "engine": "zen-llm-engine",
            "reason": "ConnectError: Connection error.",
        }

    def test_a_turn_that_fails_carries_the_reason_in_its_error_too(self) -> None:
        card = begin()
        tc.mark_failed("TimeoutError: no reply")
        assert card.error == "TimeoutError: no reply" and card.status == tc.FAILED

    def test_failures_are_bounded_and_long_reasons_cut(self) -> None:
        card = begin()
        for n in range(tc.MAX_FAILURES + 5):
            tc.note_failure("llm", "x" * 1000 + str(n))
        assert len(card.failures) == tc.MAX_FAILURES
        assert all(len(f["reason"]) <= 300 for f in card.failures)

    def test_without_a_current_card_it_does_nothing(self) -> None:
        tc.note_failure("llm", "boom")  # must not raise


class TestStages:
    def test_a_stage_is_timed_and_added(self) -> None:
        card = begin()
        with tc.stage("recon", {"chars": 120}):
            time.sleep(0.02)
        section = card.sections["recon"]
        assert section.count == 1 and section.seconds >= 0.02
        assert section.data == {"chars": 120}
        assert card.to_dict()["timings"]["recon"] >= 0.02

    @pytest.mark.asyncio
    async def test_async_stages_work_too_and_nest(self) -> None:
        card = begin()
        async with tc.stage("body"):
            async with tc.stage("debrief", {"n": 1}):
                await asyncio.sleep(0.01)
        assert set(card.sections) == {"body", "debrief"}

    def test_a_section_called_twice_adds_up(self) -> None:
        card = begin()
        tc.note("corrector", {"a": 1}, seconds=1.0)
        tc.note("corrector", {"b": 2}, seconds=0.5)
        section = card.sections["corrector"]
        assert (section.count, round(section.seconds, 2)) == (2, 1.5)
        assert section.data == {"a": 1, "b": 2}

    def test_sections_keep_the_order_they_first_appeared_in(self) -> None:
        begin()
        for name in ("recon", "body", "debrief"):
            tc.note(name)
        assert [s["name"] for s in tc.live()[0]["sections"]] == [
            "recon",
            "body",
            "debrief",
        ]

    def test_the_corrector_is_counted(self) -> None:
        card = begin()
        tc.corrector_turn()
        tc.corrector_turn(converged=True)
        assert card.to_dict()["corrector"] == {"turns": 2, "converged": True}

    def test_nothing_happens_without_a_current_card(self) -> None:
        tc.note("recon", {"x": 1})
        tc.attempt("t", True)
        tc.corrector_turn()
        with tc.stage("body"):
            pass
        assert tc.live() == [] and tc.current() is None

    def test_the_number_of_sections_is_bounded(self) -> None:
        card = begin()
        for i in range(60):
            tc.note(f"s{i}")
        assert len(card.sections) <= tc.MAX_SECTIONS
        assert card.sections["other"].count > 0

    def test_a_section_cannot_grow_without_bound(self) -> None:
        card = begin()
        for i in range(200):
            tc.note("big", {f"k{i}": "v" * 1000})
        assert len(json.dumps(card.sections["big"].data)) <= tc.MAX_SECTION_BYTES + 1100
        assert card.sections["big"].data.get("truncated") is True


class TestClean:
    def test_everything_becomes_json_safe_and_bounded(self) -> None:
        class Odd:
            def __repr__(self) -> str:
                return "<odd>"

        data = tc.clean(
            {
                "set": {1, 2},
                "nan": float("nan"),
                "odd": Odd(),
                "deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}},
                "long": "y" * 10_000,
                "many": list(range(500)),
            }
        )
        json.dumps(data)
        assert data["nan"] is None and data["odd"] == "<odd>"
        assert len(data["many"]) == tc.MAX_ITEMS and data["long"].endswith("…")


class TestLifecycle:
    def test_a_card_is_live_while_it_runs_and_has_wait_and_run_times(self) -> None:
        card = begin(queued_at=time.time() - 5, trigger={"text": "hello"})
        [shown] = tc.live()
        assert shown["status"] == "processing" and shown["id"] == card.id
        assert 4.9 <= shown["queue_seconds"] <= 6
        assert shown["processing_seconds"] is not None and shown["finished_at"] is None

    @pytest.mark.asyncio
    async def test_a_finished_card_is_saved_and_no_longer_live(self) -> None:
        card = begin(trigger={"text": "hello"})
        tc.note("body", seconds=0.2)
        tc.finish()
        await tc.drain()
        assert tc.live() == [] and tc.current() is None
        [saved] = await tc.recent()
        assert saved["id"] == card.id and saved["status"] == "processed"
        assert saved["finished_at"] is not None and saved["timings"]["body"] == 0.2

    @pytest.mark.asyncio
    async def test_a_failed_turn_is_labelled_failed_with_its_error(self) -> None:
        begin()
        tc.finish(failed=True, error="the engine timed out")
        await tc.drain()
        [saved] = await tc.recent()
        assert (saved["status"], saved["error"]) == ("failed", "the engine timed out")

    @pytest.mark.asyncio
    async def test_marking_a_failure_first_makes_the_end_a_failure(self) -> None:
        begin()
        tc.mark_failed("boom")
        tc.finish()
        await tc.drain()
        [saved] = await tc.recent()
        assert saved["status"] == "failed" and saved["error"] == "boom"

    @pytest.mark.asyncio
    async def test_recent_filters_by_status_and_text_and_is_newest_first(self) -> None:
        for text, failed in (("alpha", False), ("beta", True), ("gamma", False)):
            tc.begin(kind="chat", interface="x", trigger={"text": text})
            tc.finish(failed=failed)
            await tc.drain()
        assert [c["trigger"]["text"] for c in await tc.recent()] == [
            "gamma",
            "beta",
            "alpha",
        ]
        assert [c["trigger"]["text"] for c in await tc.recent(status="failed")] == [
            "beta"
        ]
        assert [c["trigger"]["text"] for c in await tc.recent(search="ALP")] == [
            "alpha"
        ]
        assert (await tc.get((await tc.recent(limit=1))[0]["id"]))["trigger"][
            "text"
        ] == "gamma"
        assert await tc.get("nope") is None

    @pytest.mark.asyncio
    async def test_get_finds_a_live_card_too(self) -> None:
        card = begin()
        assert (await tc.get(card.id))["status"] == "processing"

    @pytest.mark.asyncio
    async def test_tasks_created_from_the_turn_share_its_card(self) -> None:
        card = begin()

        async def detached() -> None:
            tc.note("agent", {"steps": 3})

        await asyncio.ensure_future(detached())
        assert card.sections["agent"].data == {"steps": 3}

    @pytest.mark.asyncio
    async def test_two_turns_in_two_tasks_do_not_mix(self) -> None:
        async def turn(name: str) -> str:
            card = tc.begin(kind="chat", interface=name, trigger={"text": name})
            await asyncio.sleep(0.01)
            tc.note("body", {"who": name})
            assert tc.current() is card
            return card.sections["body"].data["who"]

        assert sorted(await asyncio.gather(turn("a"), turn("b"))) == ["a", "b"]

    def test_the_ring_is_bounded(self) -> None:
        store = tc.MemoryStore(size=3)
        for i in range(10):
            asyncio.run(store.save({"id": str(i)}))
        assert [c["id"] for c in asyncio.run(store.recent(10))] == ["9", "8", "7"]

    def test_a_merged_batch_is_recorded(self) -> None:
        assert tc.begin(kind="chat", interface="x", merged=3).to_dict()["merged"] == 3


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_a_broken_store_never_raises(self) -> None:
        class Broken:
            async def save(self, card: dict) -> None:
                raise RuntimeError("disk full")

            async def recent(self, limit: int, **kw: object) -> list:
                return []

            async def get(self, card_id: str) -> None:
                return None

        tc.set_store(Broken())
        begin()
        tc.finish()
        await tc.drain()  # must not raise

    def test_an_odd_trigger_does_not_stop_the_card(self) -> None:
        card = tc.begin(kind="chat", interface="x", trigger={"obj": object()})
        assert card is not None and "obj" in card.trigger

    def test_finish_without_a_loop_is_silent(self) -> None:
        begin()
        tc.finish()  # no running loop: nothing is saved, nothing raised
        assert tc.live() == []


class TestQueueHooks:
    """The consumer's three in-flight helpers start, mark and end the card."""

    def item(self, **kw: object) -> dict:
        from types import SimpleNamespace

        base = {
            "message": SimpleNamespace(text="  hello   there "),
            "interface": "synth_webui",
            "interface_path": "synth_webui/s1",
            "chat_id": "s1",
            "chat_name": "Session 1",
            "timestamp": time.time() - 3,
        }
        base.update(kw)
        return base

    @pytest.mark.asyncio
    async def test_a_turn_through_the_queue_helpers_leaves_a_processed_card(
        self,
    ) -> None:
        from core import message_queue as mq

        mq._note_item_in_flight(self.item(), mq.PRIORITY_GENERAL)  # noqa: SLF001
        [live] = tc.live()
        assert live["interface"] == "synth_webui" and live["kind"] == "chat"
        assert (
            live["interface_path"] == "synth_webui/s1"
            and live["chat_name"] == "Session 1"
        )
        assert live["trigger"]["text"] == "hello   there"
        assert 2.5 <= live["queue_seconds"] <= 6
        tc.note("body", seconds=0.1)
        mq._clear_item_in_flight()  # noqa: SLF001
        await tc.drain()
        [saved] = await tc.recent()
        assert saved["status"] == "processed" and saved["timings"] == {"body": 0.1}

    @pytest.mark.asyncio
    async def test_a_failed_turn_is_failed_in_history(self) -> None:
        from core import message_queue as mq

        mq._note_item_in_flight(self.item(), mq.PRIORITY_GENERAL)  # noqa: SLF001
        mq._note_turn_failed()  # noqa: SLF001
        mq._clear_item_in_flight()  # noqa: SLF001
        await tc.drain()
        assert (await tc.recent())[0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_the_kind_follows_the_queue_classification(self) -> None:
        from core import message_queue as mq

        mq._note_item_in_flight(  # noqa: SLF001
            self.item(vessel_reflection=True, interface_path="vessel/minecraft"),
            mq.PRIORITY_GENERAL,
        )
        assert tc.live()[0]["kind"] == "vessel_reflection"
        mq._clear_item_in_flight()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_a_broken_item_never_breaks_the_helpers(self) -> None:
        from core import message_queue as mq

        mq._note_item_in_flight(None, None)  # noqa: SLF001
        mq._note_item_in_flight({"message": object()}, mq.PRIORITY_GENERAL)  # noqa: SLF001
        mq._clear_item_in_flight()  # noqa: SLF001


class TestTimedDecorator:
    @pytest.mark.asyncio
    async def test_it_changes_nothing_about_the_call(self) -> None:
        @tc.timed("recon")
        async def work(a: int, *, b: int = 2) -> int:
            """doc"""
            return a + b

        assert await work(1, b=5) == 6  # no card: still works
        assert work.__name__ == "work" and work.__doc__ == "doc"

        @tc.timed("recon")
        async def boom() -> None:
            raise ValueError("kept")

        begin()
        with pytest.raises(ValueError, match="kept"):
            await boom()
        assert tc.live()[0]["sections"][0]["name"] == "recon"  # still timed

    @pytest.mark.asyncio
    async def test_a_stage_is_timed_on_the_current_card(self) -> None:
        @tc.timed("recon")
        async def recon() -> str:
            await asyncio.sleep(0.02)
            return "ok"

        card = begin()
        assert await recon() == "ok"
        assert (
            card.sections["recon"].seconds >= 0.02 and card.sections["recon"].count == 1
        )

    @pytest.mark.asyncio
    async def test_corrector_calls_are_counted_with_their_outcome(self) -> None:
        outcomes = iter([None, "fixed"])

        @tc.timed("corrector", corrector=True)
        async def correct() -> str | None:
            return next(outcomes)

        card = begin()
        await correct()
        assert card.to_dict()["corrector"] == {"turns": 1, "converged": False}
        await correct()
        assert card.to_dict()["corrector"] == {"turns": 2, "converged": True}
        assert card.sections["corrector"].count == 2

    @pytest.mark.asyncio
    async def test_a_correction_inside_the_debrief_belongs_to_the_debrief(self) -> None:
        @tc.timed("corrector", corrector=True)
        async def correct() -> str:
            return "x"

        @tc.timed("debrief")
        async def debrief() -> None:
            await correct()
            await correct()

        card = begin()
        await debrief()
        assert card.corrector_turns == 0 and "corrector" not in card.sections
        assert card.sections["debrief"].data == {"corrector_turns": 1}
        assert card.active == []

    @pytest.mark.asyncio
    async def test_the_real_pipeline_functions_carry_the_timing(self) -> None:
        import inspect

        from core import debrief, recon, transport_layer

        for fn in (
            recon.gather_recon_contributions,
            debrief.run_debrief,
            transport_layer.run_corrector_middleware,
        ):
            assert inspect.iscoroutinefunction(fn)
            assert hasattr(fn, "__wrapped__")


class TestApi:
    def client(self):
        from starlette.testclient import TestClient

        from core.webui import SynthWebUIInterface

        return TestClient(SynthWebUIInterface(autostart=False).app)

    def seed(self, text: str, failed: bool) -> str:
        card = tc.begin(kind="chat", interface="synth_webui", trigger={"text": text})
        tc.note("body", seconds=0.5)
        tc.finish(failed=failed, error="boom" if failed else None)
        data = {"id": card.id, **card.to_dict()}
        asyncio.run(tc.get_store().save(data))
        return card.id

    def test_the_live_turns_are_listed(self) -> None:
        begin(trigger={"text": "running now"})
        body = self.client().get("/api/turns/live").json()
        assert body["success"] is True
        [turn] = body["turns"]
        assert (
            turn["status"] == "processing" and turn["trigger"]["text"] == "running now"
        )
        assert turn["sections"] == [] and "generated_at" in body

    def test_finished_turns_come_newest_first_with_their_label(self) -> None:
        self.seed("first", failed=False)
        self.seed("second", failed=True)
        body = self.client().get("/api/turns").json()
        assert [t["trigger"]["text"] for t in body["turns"]] == ["second", "first"]
        assert [t["status"] for t in body["turns"]] == ["failed", "processed"]
        assert body["total_count"] == 2

    def test_the_list_can_be_filtered(self) -> None:
        self.seed("alpha", failed=False)
        self.seed("beta", failed=True)
        client = self.client()
        assert [
            t["trigger"]["text"]
            for t in client.get("/api/turns?status=failed").json()["turns"]
        ] == ["beta"]
        assert [
            t["trigger"]["text"]
            for t in client.get("/api/turns?search=alp").json()["turns"]
        ] == ["alpha"]
        assert client.get("/api/turns?status=queued").status_code == 400
        assert client.get("/api/turns?limit=abc").json()["success"] is True

    def test_one_turn_can_be_read_live_or_finished(self) -> None:
        done = self.seed("done", failed=False)
        live = begin(trigger={"text": "live"}).id
        client = self.client()
        assert client.get(f"/api/turns/{done}").json()["turn"]["status"] == "processed"
        assert client.get(f"/api/turns/{live}").json()["turn"]["status"] == "processing"
        assert client.get("/api/turns/nope").status_code == 404

    def test_the_old_endpoints_are_gone(self) -> None:
        client = self.client()
        assert client.get("/api/reason-trail").status_code == 404
        assert client.get("/api/history/grillo").status_code == 404

    def test_the_reason_summary_becomes_two_sections(self) -> None:
        card = begin()
        tc.note_reason(
            {
                "memories": [{"id": 1}],
                "diary_sources": [],
                "emotion": "joy",
                "beat_type": "x",
            }
        )
        assert card.sections["memories"].data["recalled"] == [{"id": 1}]
        assert card.sections["emotions"].data["emotion"] == "joy"
