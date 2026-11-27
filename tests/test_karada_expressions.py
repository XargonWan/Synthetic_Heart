"""Facial expressions are Karada's: the [em_NAME:INTENSITY] tags of a reply, played on the face."""

from __future__ import annotations

import asyncio
import struct
import tempfile
import wave
from pathlib import Path

import pytest

from core import karada_expressions as ke
from core.animation_handler import KaradaStateServer
from core.facial_expression_parser import FacialExpressionEvent


class DummyKarada(KaradaStateServer):
    def __init__(self, clients: bool = True) -> None:
        super().__init__(webui=None)
        self.sent: list[tuple[str | None, float]] = []
        self.targets: list[dict | None] = []
        self._clients = clients

    def has_connected_clients(self) -> bool:
        return self._clients

    async def push_face_expression(self, name, intensity, targets=None):
        self.sent.append((name, intensity))
        self.targets.append(targets)


@pytest.fixture
def persona(monkeypatch):
    """A persona whose pace and catalogue the test chooses."""

    def use(chars_per_sec: float = 1000, section: dict | None = None):
        monkeypatch.setattr(
            ke, "persona_expressions", lambda: (chars_per_sec, section or {})
        )

    use()
    return use


class TestTimeline:
    @pytest.mark.asyncio
    async def test_the_events_are_played_in_order_then_the_face_is_cleared(
        self,
    ) -> None:
        karada = DummyKarada()
        events = [
            FacialExpressionEvent(position=0, name="smile", intensity=0.5),
            FacialExpressionEvent(position=5, name="sad", intensity=0.2),
        ]
        await karada.play_expression_timeline(events, total_chars=10, chars_per_sec=100)
        assert karada.sent == [("smile", 0.5), ("sad", 0.2), (None, 0)]

    @pytest.mark.asyncio
    async def test_the_real_audio_length_replaces_the_estimate(self) -> None:
        karada = DummyKarada()
        events = [
            FacialExpressionEvent(position=0, name="smile", intensity=0.5),
            FacialExpressionEvent(position=5, name="sad", intensity=0.2),
        ]
        t0 = asyncio.get_event_loop().time()
        await karada.play_expression_timeline(
            events, total_chars=10, chars_per_sec=1, audio_duration_s=0.2
        )
        assert (
            asyncio.get_event_loop().time() - t0 < 1.0
        )  # not the 10 s the estimate says
        assert karada.sent[-1] == (None, 0)

    @pytest.mark.asyncio
    async def test_the_last_expression_is_held_until_the_end_of_the_audio(self) -> None:
        karada = DummyKarada()
        t0 = asyncio.get_event_loop().time()
        await karada.play_expression_timeline(
            [FacialExpressionEvent(position=0, name="grin", intensity=0.9)],
            total_chars=100,
            chars_per_sec=100,
        )
        assert asyncio.get_event_loop().time() - t0 >= 0.8
        assert karada.sent[0] == ("grin", 0.9) and karada.sent[-1] == (None, 0)

    @pytest.mark.asyncio
    async def test_a_bare_em_is_sent_as_a_reset(self) -> None:
        karada = DummyKarada()
        await karada.play_expression_timeline(
            [
                FacialExpressionEvent(position=0, name="smile", intensity=0.8),
                FacialExpressionEvent(position=5, name=None, intensity=1.0),
            ],
            total_chars=10,
            chars_per_sec=100,
        )
        assert karada.sent[:2] == [("smile", 0.8), (None, 1.0)]

    @pytest.mark.asyncio
    async def test_targets_come_from_the_persona_catalogue_scaled_by_intensity(
        self,
    ) -> None:
        karada = DummyKarada()
        section = {
            "smile": {"targets": {"happy": 1.0, "aa": 0.5}},
            "neutral": {"targets": {}},
        }
        await karada.play_expression_timeline(
            [
                FacialExpressionEvent(position=0, name="smile", intensity=0.5),
                FacialExpressionEvent(position=1, name="neutral", intensity=1.0),
            ],
            total_chars=2,
            chars_per_sec=1000,
            expr_section=section,
        )
        assert karada.targets[0] == {"happy": 0.5, "aa": 0.25}
        assert karada.sent[1] == (None, 0)  # an expression with no targets clears


class TestScheduling:
    @pytest.mark.asyncio
    async def test_tags_are_stripped_and_played_in_the_background(
        self, persona
    ) -> None:
        karada = DummyKarada()
        clean = karada.apply_expression_tags("Hello [em_grin:0.9] world")
        assert clean == "Hello  world"
        for _ in range(100):
            if ("grin", 0.9) in karada.sent and karada.sent[-1] == (None, 0):
                break
            await asyncio.sleep(0.02)
        assert ("grin", 0.9) in karada.sent

    @pytest.mark.asyncio
    async def test_nothing_is_played_when_nobody_is_watching(self, persona) -> None:
        karada = DummyKarada(clients=False)
        assert karada.apply_expression_tags("Hi [em_grin:0.9]") == "Hi "
        assert (
            karada.schedule_expressions(
                [FacialExpressionEvent(position=0, name="grin", intensity=1.0)], 5
            )
            is False
        )
        await asyncio.sleep(0.05)
        assert karada.sent == []

    def test_text_without_tags_is_untouched(self) -> None:
        assert DummyKarada().apply_expression_tags("plain") == "plain"


class TestGuidance:
    def test_it_names_the_personas_expressions(self, persona) -> None:
        persona(section={"wink": {}, "pout": {}})
        text = DummyKarada().expression_guidance()
        assert "Available: wink, pout" in text and "[em_NAME:INTENSITY]" in text

    def test_without_a_persona_the_defaults_are_named(self, persona) -> None:
        persona(section={})
        assert "smile, grin, sad" in DummyKarada().expression_guidance()

    def test_it_is_part_of_the_static_injections_of_every_turn(self, persona) -> None:
        from core.action_parser import _add_core_injections

        injections = _add_core_injections({})
        assert "[em_NAME:INTENSITY]" in injections["facial_expression_guidance"]


class TestPersonaSettings:
    def test_a_missing_persona_gives_the_default_pace_and_no_catalogue(
        self, monkeypatch
    ) -> None:
        import core.persona_manager as pm

        monkeypatch.setattr(pm, "get_persona_manager", lambda: None)
        assert ke.persona_expressions() == (float(ke.DEFAULT_CHARS_PER_SEC), {})

    def test_the_persona_sets_the_pace_and_the_catalogue(self, monkeypatch) -> None:
        import core.persona_manager as pm

        class PM:
            _current_persona = type("P", (), {"name": "Rei"})()

            def _load_persona_json(self, name):
                return {
                    "facial_expression_chars_per_sec": 20,
                    "facial_expressions": {"smile": {"targets": {"a": 1}}},
                }

        monkeypatch.setattr(pm, "get_persona_manager", lambda: PM())
        rate, section = ke.persona_expressions()
        assert rate == 20.0 and list(section) == ["smile"]


def test_the_expression_plugin_is_gone() -> None:
    root = Path(__file__).resolve().parent.parent
    assert not (root / "plugins" / "facial_expression_plugin").exists()


def test_get_wav_duration() -> None:
    """The real length of a clip, which sets the pace of its expressions."""
    from plugins.vox_plugin import _get_wav_duration

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = Path(f.name)
    rate = 22050
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{rate}h", *([0] * rate)))
    duration = _get_wav_duration(path)
    path.unlink()
    assert duration is not None and abs(duration - 1.0) < 0.01


def test_get_wav_duration_bad_file() -> None:
    from plugins.vox_plugin import _get_wav_duration

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"not a wav file")
        path = Path(f.name)
    assert _get_wav_duration(path) is None
    path.unlink()
