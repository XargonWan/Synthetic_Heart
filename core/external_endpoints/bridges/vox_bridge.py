# core/external_endpoints/bridges/vox_bridge.py
"""Vox (TTS) bridge for external endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugins.vox_base import VoxEngineBase

if TYPE_CHECKING:
    from core.external_endpoints.adapters.base import BaseProtocolAdapter
    from core.external_endpoints.models import ExternalEndpoint


class ExternalVoxEngine(VoxEngineBase):
    """VoxEngineBase implementation backed by an external endpoint adapter."""

    def __init__(
        self,
        endpoint: "ExternalEndpoint",
        adapter: "BaseProtocolAdapter | None",
    ) -> None:
        self._endpoint = endpoint
        self._adapter = adapter
        if self._adapter is not None:
            self._adapter._engine_label = endpoint.name or "vox_bridge"
        self.display_name = f"{endpoint.display_label or endpoint.name} (TTS)"

    @property
    def output_format(self) -> str:
        extra = self._endpoint.extra_config or {}
        return str(extra.get("tts_output_format", extra.get("output_format", "wav")))

    @property
    def sample_rate(self) -> int:
        extra = self._endpoint.extra_config or {}
        try:
            return int(extra.get("tts_sample_rate", extra.get("sample_rate", 22050)))
        except Exception:
            return 22050

    @property
    def channels(self) -> int:
        extra = self._endpoint.extra_config or {}
        try:
            return int(extra.get("tts_channels", extra.get("channels", 1)))
        except Exception:
            return 1

    def get_speakers(self) -> list[dict]:
        """Return the endpoint's available voices, if the adapter exposes them.

        Adapters that support voice discovery (e.g. Fish Audio via ``GET
        /model``) implement an async ``list_speakers`` returning
        ``{"reference_id", "title", "language"}`` dicts. We map those onto the
        base ``get_speakers`` contract (``{"code", "name", "language"}``) where
        ``code`` is the ``reference_id`` — that is what the WebUI persists into
        ``extra_config["tts_reference_id"]`` and the adapter reads back at
        synthesis time. Adapters without ``list_speakers`` inherit the empty
        default.
        """
        import asyncio

        adapter = self._adapter
        if adapter is None:
            return []
        list_speakers = getattr(adapter, "list_speakers", None)
        if not callable(list_speakers):
            return []

        coro = list_speakers()
        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is not None:
                import concurrent.futures

                future: concurrent.futures.Future[list[dict[str, Any]]] = (
                    concurrent.futures.Future()
                )

                async def _run() -> None:
                    try:
                        future.set_result(await coro)
                    except Exception as exc:
                        future.set_exception(exc)

                asyncio.ensure_future(_run())
                raw = future.result(timeout=30)
            else:
                raw = asyncio.run(coro)
        except Exception as exc:
            from core.logging_utils import log_warning

            log_warning(
                f"[vox_bridge:{self._endpoint.name}] get_speakers failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return []

        speakers: list[dict] = []
        for entry in raw or []:
            if not isinstance(entry, dict):
                continue
            code = entry.get("reference_id") or entry.get("code")
            name = entry.get("title") or entry.get("name") or code
            if not code:
                continue
            speaker: dict[str, Any] = {"code": str(code), "name": str(name)}
            if entry.get("language"):
                speaker["language"] = entry["language"]
            # Preserve the hierarchy tier (Manually added / My Voices /
            # Bookmarks / Default Voices) so the WebUI can group + order the
            # picker. Adapters that don't tier their voices simply omit these.
            if entry.get("tier") is not None:
                speaker["tier"] = entry["tier"]
            if entry.get("tier_label"):
                speaker["tier_label"] = entry["tier_label"]
            speakers.append(speaker)
        return speakers

    def sample(self, speaker: str) -> bytes:
        """Synthesize a short preview clip for ``speaker``.

        The WebUI voice picker calls ``/api/vox/sample`` to preview a voice
        before saving it. External TTS endpoints (e.g. Fish Audio) have no
        pre-rendered samples, so we synthesize a short fixed phrase on demand
        with the requested voice — this is what makes the preview work "like
        Kitten" for any endpoint that exposes ``get_speakers``. Raises
        ``NotImplementedError`` when synthesis is unavailable so the endpoint
        returns 404 and the UI silently skips the preview.
        """
        if self._adapter is None:
            raise NotImplementedError("engine does not provide samples")
        audio = self.generate_tts("Hello, this is a voice preview.", voice=speaker)
        if not audio:
            raise NotImplementedError("engine did not return a sample")
        return audio

    def _runtime_selected_model(self) -> str | None:
        """Return the TTS model chosen at runtime via the WebUI, if any.

        The Vox model dropdown persists its selection to the ``VOX_DEFAULT_MODEL``
        config key. That value only applies to this endpoint when the selection
        is one of the endpoint's own ``available_models`` (otherwise it belongs
        to a different Vox engine and must be ignored).
        """
        try:
            from core.config_manager import config_registry

            selected = config_registry.get_value("VOX_DEFAULT_MODEL", None)
        except Exception:
            return None
        if not selected:
            return None
        selected = str(selected).strip()
        if not selected:
            return None
        available = self._endpoint.available_models or []
        if available and selected not in available:
            return None
        return selected

    def _runtime_selected_voice(self) -> str | None:
        """Return the voice chosen at runtime via the WebUI voice picker.

        The WebUI persists the selected speaker code to the ``<ENGINE>_VOICE``
        config key (``ENGINE`` being this endpoint's name, uppercased) — the
        same convention the ``VoiceSettings`` component uses to save and
        restore the choice. For Fish Audio that code is the voice's
        ``reference_id`` exposed by ``get_speakers``. Returns ``None`` when no
        selection has been made.
        """
        name = getattr(self._endpoint, "name", None)
        if not name:
            return None
        key = f"{str(name).upper()}_VOICE"
        try:
            from core.config_manager import config_registry

            selected = config_registry.get_value(key, None)
        except Exception:
            return None
        if not selected:
            return None
        selected = str(selected).strip()
        return selected or None

    def _model_settings(self, model: str | None) -> dict[str, Any]:
        """Return the per-model TTS settings for ``model``, if configured.

        Each TTS model can carry its own voice / language / synthesis
        parameters under ``extra_config["tts_model_settings"][<model>]``.
        This lets, for example, ``kitten-tts-nano`` use ``{"voice": "Luna"}``
        while ``chatterbox_multilingual`` uses ``{"language": "it",
        "generation_options": {"pitch": 1.0, "speed": 0.9}}`` on the same
        endpoint. Returns an empty dict when nothing is configured for the
        model.
        """
        if not model:
            return {}
        extra = self._endpoint.extra_config or {}
        per_model = extra.get("tts_model_settings")
        if not isinstance(per_model, dict):
            return {}
        settings = per_model.get(model)
        if not isinstance(settings, dict):
            return {}
        return settings

    def generate_tts(
        self,
        text: str,
        emotion: str | None = None,
        **kwargs: Any,
    ) -> bytes | None:
        """Synchronous wrapper – runs the async adapter call in the event loop."""
        import asyncio

        if self._adapter is None:
            return None

        extra = self._endpoint.extra_config or {}

        # Resolve the TTS model for this endpoint.  Priority order:
        #   1. an explicit ``model`` passed by the caller;
        #   2. the runtime selection stored in the ``VOX_DEFAULT_MODEL`` config
        #      key (this is what the WebUI Vox model dropdown writes when the
        #      user picks a model, e.g. ``chatterbox_multilingual``);
        #   3. a dedicated ``tts_model`` pinned in ``extra_config``;
        #   4. the endpoint's ``default_model`` as a last resort.
        # Multi-modal endpoints (e.g. Harmony) reserve ``default_model`` for the
        # cortex/text engine, so it is only a fallback here.
        runtime_model = self._runtime_selected_model()
        if "model" not in kwargs:
            tts_model = (
                runtime_model or extra.get("tts_model") or self._endpoint.default_model
            )
            if tts_model:
                kwargs["model"] = tts_model

        # The model actually being synthesised with (used to look up its own
        # per-model voice/language/parameter settings).
        active_model = kwargs.get("model")

        # Per-model settings take precedence: each TTS model can define its own
        # voice, language and synthesis parameters (e.g. KittenTTS -> voice
        # "Luna"; a multilingual model -> language "it" + generation_options
        # with pitch/speed). See ``_model_settings``.
        model_settings = self._model_settings(active_model)

        # Whether the *legacy* endpoint-level pins (``tts_voice`` /
        # ``tts_language`` in ``extra_config``) apply. Those describe a single
        # pinned model, so they are only honoured when the active model IS that
        # pinned model (or when no model was selected at runtime). Any per-model
        # settings always win over these legacy pins.
        pinned_model = extra.get("tts_model")
        pin_applies = runtime_model is None or (
            pinned_model is not None and runtime_model == pinned_model
        )

        # --- Language ---------------------------------------------------
        # Some single-speaker TTS models (e.g. KittenTTS) accept only a fixed
        # set of languages (often just ``default``); sending the caller's
        # auto-detected language would make the model reject the request and
        # produce no audio. A per-model ``language`` is a hard constraint for
        # that model; the legacy ``tts_language`` pin is the fallback.
        if "language" in model_settings:
            kwargs["language"] = model_settings["language"]
        elif extra.get("tts_language") and pin_applies:
            kwargs["language"] = extra["tts_language"]

        # --- Voice ------------------------------------------------------
        # A voice name is only valid for the model it was configured for; other
        # models reject or hang on an unknown voice. Resolution order:
        #   1. an explicit ``voice`` from the caller (always wins);
        #   2. the per-model ``voice`` for the active model;
        #   3. the legacy endpoint pin, but only when it applies to this model.
        # Popped from ``kwargs`` so it is not forwarded twice (the adapter
        # takes ``voice`` as a dedicated keyword argument below).
        voice = kwargs.pop("voice", None)
        if voice is None:
            if "voice" in model_settings:
                voice = model_settings["voice"]
            else:
                # Voice chosen at runtime via the WebUI voice picker. The
                # picker persists the selected speaker code (for Fish, the
                # ``reference_id`` returned by ``get_speakers``) into the
                # ``<ENGINE>_VOICE`` config key. Honour it before the legacy
                # ``tts_voice`` / ``tts_reference_id`` endpoint pins so the
                # WebUI selection actually reaches synthesis.
                runtime_voice = self._runtime_selected_voice()
                if runtime_voice:
                    voice = runtime_voice
                elif pin_applies:
                    voice = extra.get("tts_voice") or extra.get("tts_reference_id")

        # --- Extra synthesis parameters --------------------------------
        # Any other per-model settings (e.g. ``mode``, ``generation_options``
        # carrying pitch/speed, ``input_embedding`` for cloning models) are
        # forwarded verbatim to the adapter, without overriding an explicit
        # value already supplied by the caller. ``voice``/``language`` are
        # handled above and skipped here.
        for key, value in model_settings.items():
            if key in ("voice", "language"):
                continue
            kwargs.setdefault(key, value)

        # Tell the adapter which container format to request. The adapter
        # otherwise defaults to a lossy format (e.g. mp3) whose raw bytes are
        # NOT RIFF/WAV; downstream ``_write_audio`` then mistakes them for raw
        # PCM and wraps them in a WAV header, producing a corrupted file that
        # only plays back its first decodable fragment (truncated audio). By
        # forwarding this endpoint's ``output_format`` (``wav`` by default) the
        # adapter returns proper WAV bytes that are written verbatim.
        if "format" not in kwargs:
            kwargs["format"] = self.output_format

        coro = self._adapter.generate_tts(text, voice=voice, **kwargs)
        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is not None:
                # If called from an already-running loop (e.g. FastAPI), schedule
                # it as a task and block synchronously via a Future.
                import concurrent.futures

                future: concurrent.futures.Future[bytes | None] = (
                    concurrent.futures.Future()
                )

                async def _run() -> None:
                    try:
                        result = await coro
                        future.set_result(result)
                    except Exception as exc:
                        future.set_exception(exc)

                asyncio.ensure_future(_run())
                return future.result(timeout=60)
            else:
                # No running loop in this thread (e.g. called via
                # ``asyncio.to_thread``): run the coroutine to completion here.
                return asyncio.run(coro)
        except Exception as exc:
            # Do NOT swallow silently: a failed TTS call (e.g. an unavailable
            # model timing out on the backend, or a rejected voice/language)
            # otherwise degrades to a text-only reply with no trace of why.
            from core.logging_utils import log_warning

            log_warning(
                f"[vox_bridge:{self._endpoint.name}] generate_tts failed "
                f"(model={kwargs.get('model')!r}, voice={voice!r}, "
                f"language={kwargs.get('language')!r}): "
                f"{type(exc).__name__}: {exc}"
            )
            return None


# Required by VoxRegistry::load_engine() when loading via module path
ENGINE_CLASS = ExternalVoxEngine
