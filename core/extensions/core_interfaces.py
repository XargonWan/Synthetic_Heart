"""The Core's real services behind the interface ports (platform milestone M3).

* :class:`CoreInterfaceHost` registers a :class:`RemoteInterface` through the same
  functions an in-process interface uses (``register_interface`` for the registry,
  capabilities and actions; ``CoreInitializer.register_interface`` to activate it
  and show it in the WebUI) and refreshes the action prompt, exactly like the
  Core's own runtime enable/disable does. Removing it undoes each of those steps.
  Nothing is persisted: an extension starting or stopping is not the user toggling
  an interface, so no ``INTERFACE_ENABLED__*`` flag is written.
* :class:`CoreMessaging` does for a remote interface what every in-process
  interface does for an incoming message: record it in the chat history, track the
  path, answer ``/commands`` and put the rest on ``message_queue``.

Every Core import is inside a function, so importing this module starts nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.extensions.interfaces import InterfacePorts, NameResolver
from core.interface_traits import InterfaceTraits

if TYPE_CHECKING:
    from core.extensions.interfaces import RemoteInterface


class CoreInterfaceHost:
    """``InterfaceHost`` backed by the Core's registries."""

    def exists(self, name: str) -> bool:
        from core.core_initializer import INTERFACE_REGISTRY

        return name in INTERFACE_REGISTRY

    async def register(self, name: str, interface: RemoteInterface) -> None:
        from core.core_initializer import core_initializer, register_interface

        register_interface(name, interface)
        core_initializer.register_interface(name)
        core_initializer._invalidate_action_caches()  # noqa: SLF001
        await core_initializer._build_actions_block()  # noqa: SLF001

    async def unregister(self, name: str, interface: RemoteInterface) -> None:
        from core.core_initializer import (
            INTERFACE_REGISTRY,
            ComponentStatus,
            core_initializer,
        )
        from core.interfaces_registry import get_interface_registry

        # Only undo what this very object registered.
        if INTERFACE_REGISTRY.get(name) is not interface:
            return
        core_initializer._unregister_plugin_actions(interface)  # noqa: SLF001
        INTERFACE_REGISTRY.pop(name, None)
        if name in core_initializer.active_interfaces:
            core_initializer.active_interfaces.remove(name)
        info = core_initializer.components.get(name)
        if info is not None:
            info.status = ComponentStatus.SKIPPED
            info.actions = []
            info.details = "Extension stopped"
        registry = get_interface_registry()
        trainer = registry.get_trainer_id(name)
        registry.unregister_interface(name)  # also drops capabilities
        if trainer is not None:  # trainer ids come from configuration: keep them
            registry.set_trainer_id(name, trainer)
        core_initializer._invalidate_action_caches()  # noqa: SLF001
        await core_initializer._build_actions_block()  # noqa: SLF001

    def interface_enabled(self, name: str) -> None:
        """An interface that was off can deliver now: bring its avatar up to date."""
        import asyncio

        from core import synth_avatar

        async def sync() -> None:
            try:
                await synth_avatar.sync_interface(name)
            except Exception:  # noqa: BLE001 - never block the interface for a picture
                return

        try:
            asyncio.get_running_loop().create_task(sync())
        except RuntimeError:
            return

    def set_traits(self, name: str, traits: InterfaceTraits | None) -> None:
        from core import interface_traits

        if traits is None:
            interface_traits.unregister_traits(name)
        else:
            interface_traits.register_traits(name, traits)

    def set_name_resolver(self, name: str, resolver: NameResolver | None) -> None:
        from core import interface_paths

        if resolver is not None:
            interface_paths.set_name_resolver(name, resolver)
        else:
            interface_paths._NAME_RESOLVERS.pop(name, None)  # noqa: SLF001


class CoreMessaging:
    """``MessagingPort`` backed by the Core's chat pipeline."""

    async def record(
        self,
        interface_path: str,
        text: str,
        sender_name: str,
        sender_id: str,
        message_id: str | None,
        metadata: dict[str, Any] | None,
        timestamp: Any = None,
    ) -> None:
        from core.chat_context_manager import add_message_to_context
        from core.logging_utils import log_warning

        extra: dict[str, Any] = {}
        if timestamp is not None:
            extra["timestamp"] = timestamp.isoformat()
        try:
            await add_message_to_context(
                interface_path=interface_path,
                message_text=text,
                sender_name=sender_name,
                sender_id=sender_id,
                message_id=message_id,  # type: ignore[arg-type]
                metadata=metadata,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 - history must not block the turn
            log_warning(f"[extensions] could not record message in history: {exc}")

    async def touch(
        self, interface_path: str, chat_id: Any, thread_id: Any, bot: Any
    ) -> None:
        from core.interface_paths import resolve_and_touch
        from core.logging_utils import log_warning

        try:
            await resolve_and_touch(interface_path, chat_id, thread_id, bot=bot)
        except Exception as exc:  # noqa: BLE001
            log_warning(f"[extensions] could not update chat link names: {exc}")

    async def seen(self, interface_path: str, sender_id: str, timestamp: Any) -> bool:
        from core.chat_history_cache import chat_message_exists

        return await chat_message_exists(interface_path, sender_id, timestamp)

    async def enqueue_and_wait(
        self,
        bot: Any,
        message: Any,
        interface_id: str,
        *,
        context_memory: Any,
        history_scope: str | None,
        timeout: float | None,
    ) -> str | None:
        from core import message_queue

        return await message_queue.enqueue_and_wait(
            bot=bot,
            message=message,
            context_memory=context_memory,
            history_scope=history_scope,
            priority=False,
            interface_id=interface_id,
            skip_mention_check=True,
            original_message=message,
            timeout=timeout,
        )

    def agent_in_flight(self, interface_path: str) -> bool:
        try:
            from core.agent_router import has_inflight_agent_turn

            return bool(has_inflight_agent_turn(interface_path))
        except Exception:  # noqa: BLE001
            return False

    async def set_processing(self, interface_path: str, processing: bool) -> None:
        try:
            from core.session_meta import set_session_meta

            await set_session_meta(interface_path, {"processing": processing})
        except Exception:  # noqa: BLE001 - an indicator, never a failure
            return

    def trainer_of(self, interface_id: str) -> str | None:
        from core.interfaces_registry import get_interface_registry

        raw = get_interface_registry().get_trainer_id(interface_id)
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else None
        return None if raw is None else str(raw)

    async def learn_alias(self, interface_id: str, message: Any) -> None:
        from core.interface_aliases import remember_message_alias

        await remember_message_alias(interface_id, message)

    async def command(
        self, text: str, user_id: str, interface_id: str, context: dict[str, Any]
    ) -> str | None:
        from core.command_registry import handle_command_message

        return await handle_command_message(
            text,
            user_id=user_id,
            interface_id=interface_id,
            interface_context=context,
        )

    async def enqueue(
        self, bot: Any, message: Any, interface_id: str, skip_mention_check: bool
    ) -> None:
        from core import message_queue
        from core.chat_attention import apply_text_triggers

        try:
            me = await bot.get_me()
            username = getattr(me, "username", None)
        except Exception:
            username = None
        apply_text_triggers(
            getattr(message, "chat_id", None), getattr(message, "text", ""), username
        )
        await message_queue.enqueue(
            bot,
            message,
            interface_id=interface_id,
            skip_mention_check=skip_mention_check,
        )


def core_outbound_resolver(raw_path: str) -> tuple[Path | None, str | None]:
    """The Core's own outbound sandbox rule (allowed roots, no credential files)."""
    from core.outbound_file_utils import resolve_safe_outbound_path

    return resolve_safe_outbound_path(raw_path)


def core_classifier(path: Path) -> tuple[str, str]:
    """``(kind, mime)`` as the Core's interfaces classify outbound media."""
    from core.outbound_file_utils import classify_media, guess_mime_type

    return classify_media(path), guess_mime_type(path)


def core_avatar() -> tuple[bytes, str] | None:
    """The synth avatar the WebUI stores, as ``(png, version)``."""
    from core import synth_avatar

    return synth_avatar.load_avatar()


def core_info() -> dict[str, Any]:
    """The SyntH's name and the union of what its engines provide (booleans only)."""
    from core.app_paths import in_container
    from core.config_manager import config_registry

    try:
        name = str(config_registry.get_value("SYNTH_NAME", "") or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    capabilities: dict[str, bool] = {}

    def merge(caps: Any) -> None:
        if isinstance(caps, dict):
            for key, value in caps.items():
                if isinstance(key, str) and bool(value):
                    capabilities[key] = True

    try:
        from core.cortex_registry import get_cortex_registry

        registry = get_cortex_registry()
        for engine in registry.get_available_engines() or []:
            merge(registry._engine_meta.get(engine, {}).get("capabilities", {}))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass
    for module_path, attr in (
        ("core.vox_registry", "VOX_REGISTRY"),
        ("core.auris_registry", "AURIS_REGISTRY"),
        ("core.iris_registry", "IRIS_REGISTRY"),
    ):
        try:
            engines = __import__(module_path, fromlist=[attr])
            reg = getattr(engines, attr)
            for engine in reg.get_available_engines() or []:
                merge(reg.get_engine_meta(engine).get("capabilities", {}))
        except Exception:  # noqa: BLE001
            continue
    return {
        "synth_name": name,
        "capabilities": capabilities,
        "in_container": in_container(),
    }


def core_call_ports() -> Any:
    """The call services of the running Core, for ``core.extensions.calls``."""
    from core.call_audio import get_call_audio_pipeline
    from core.call_manager import get_call_manager
    from core.call_video import VideoSettings, get_call_video_hub, jpeg_to_i420
    from core.extensions.calls import CallPorts

    return CallPorts(
        manager=get_call_manager,
        pipeline=get_call_audio_pipeline,
        video=get_call_video_hub,
        jpeg_to_i420=jpeg_to_i420,
        video_settings=VideoSettings.from_config,
    )


def core_interface_ports() -> InterfacePorts:
    """The interface ports backed by the running Core."""
    return InterfacePorts(host=CoreInterfaceHost(), messaging=CoreMessaging())


class CoreEndpointPort:
    """The endpoint API over the Core's external endpoint registry."""

    def _registry(self) -> Any:
        from core.external_endpoints.registry import get_external_endpoint_registry

        return get_external_endpoint_registry()

    async def get(self, name: str) -> Any:
        from core.extensions.endpoints import EndpointInfo

        found = await self._registry().get_endpoint(name)
        if found is None:
            return None
        return EndpointInfo(
            id=found.id,
            name=found.name,
            base_url=found.base_url,
            has_key=bool(found.api_key_enc),
            extra_config=dict(found.extra_config or {}),
        )

    async def count(self) -> int:
        return len(await self._registry().list_endpoints())

    async def add(
        self,
        name: str,
        display_label: str,
        base_url: str,
        protocol: str,
        extra_config: dict[str, Any],
        subsystem_map: dict[str, bool],
    ) -> int:
        created = await self._registry().add_endpoint(
            name=name,
            base_url=base_url,
            protocol=protocol,
            display_label=display_label,
            extra_config=extra_config,
            subsystem_map=subsystem_map,
        )
        return int(created.id)

    async def update(
        self,
        endpoint_id: int,
        *,
        base_url: str | None = None,
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        fields: dict[str, Any] = {}
        if base_url is not None:
            fields["base_url"] = base_url
        if extra_config is not None:
            fields["extra_config"] = extra_config
        await self._registry().update_endpoint(endpoint_id, **fields)


def core_endpoint_port() -> CoreEndpointPort:
    return CoreEndpointPort()


class CoreTurns:
    """``TurnPort`` backed by the Core's message queue.

    The turn is queued like any other low-priority work (a Grillo beat, a reminder):
    its own ``context_memory`` (never the shared one), no chat history, and the
    extension's context under :data:`core.extensions.turns.CONTEXT_KEY`.
    """

    async def request(
        self,
        *,
        extension_id: str,
        interface_id: str,
        path: str,
        text: str,
        sender_name: str,
        priority: str,
        context: dict[str, Any],
        allowed_actions: list[str],
        history_recent_max: int,
    ) -> None:
        import asyncio
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from core import message_queue
        from core.extensions.interfaces import CONTEXT_KEY

        bands = {
            "background": message_queue.PRIORITY_BACKGROUND,
            "low": message_queue.PRIORITY_LOW,
            "ambient": message_queue.PRIORITY_AMBIENT,
            "general": message_queue.PRIORITY_GENERAL,
            "elevated": message_queue.PRIORITY_ELEVATED,
        }
        chat_part = path.split("/")[1] if "/" in path else path
        try:
            chat_id: Any = int(chat_part)
        except ValueError:
            chat_id = chat_part
        message = SimpleNamespace()
        message.chat_id = chat_id
        message.message_id = (
            f"ext_turn_{interface_id}_{asyncio.get_running_loop().time()}"
        )
        message.text = text
        message.from_user = SimpleNamespace(
            id=chat_id,
            username=interface_id,
            full_name=sender_name,
            first_name=sender_name,
        )
        message.chat = SimpleNamespace(id=chat_id, type="internal")
        message.date = datetime.now(timezone.utc)
        context_memory: dict[str, Any] = {
            "skip_history": True,
            "history_recent_max": history_recent_max,
            "beat_type": "extension_turn",
            "allowed_action_types": list(allowed_actions),
            CONTEXT_KEY: {
                "extension_id": extension_id,
                "interface_id": interface_id,
                "context": context,
            },
        }
        await message_queue.enqueue_low_priority(
            None,
            message,
            context_memory=context_memory,
            interface_id=interface_id,
            original_message=None,
            priority=bands[priority],
        )


class CoreVoice:
    """``VoicePort`` backed by the Core's Vox plugin."""

    @staticmethod
    def _vox() -> Any | None:
        from core.core_initializer import PLUGIN_REGISTRY

        return PLUGIN_REGISTRY.get("vox_plugin") or PLUGIN_REGISTRY.get("vox")

    async def synthesize(self, text: str, engine: str) -> str | None:
        import os

        from core.logging_utils import log_warning

        vox = self._vox()
        if vox is None:
            log_warning("[extensions] voice.synthesize: no Vox plugin")
            return None
        spoken = text.strip()
        if not spoken:
            spoken = "A track transition."
        result = await vox.speak(
            text=spoken, engine_name=engine or None, generate_only=True
        )
        path = result.get("audio_path") if isinstance(result, dict) else None
        return path if path and os.path.isfile(path) else None

    async def play(self, audio_path: str, text: str, engine: str) -> bool:
        vox = self._vox()
        if vox is None or not hasattr(vox, "broadcast_audio_to_webui"):
            return False
        return bool(
            await vox.broadcast_audio_to_webui(
                audio_path=audio_path, text=text or None, engine_name=engine or None
            )
        )

    async def transcribe(
        self, audio_path: str, mime_type: str
    ) -> dict[str, Any] | None:
        from core.core_initializer import PLUGIN_REGISTRY
        from core.logging_utils import log_warning

        auris = PLUGIN_REGISTRY.get("auris_plugin")
        if auris is None or not hasattr(auris, "transcribe_audio"):
            log_warning("[extensions] voice.transcribe: no Auris plugin")
            return None
        result = await auris.transcribe_audio(audio_path, mime_type or None)
        text = str(getattr(result, "text", "") or "").strip()
        if not text:
            return None
        return {"text": text, "language": getattr(result, "language", None)}


class CoreReconHost:
    """``ReconHost`` backed by the Core's plugin registry, where Recon looks."""

    def add(self, name: str, plugin: object) -> None:
        from core.core_initializer import PLUGIN_REGISTRY

        PLUGIN_REGISTRY[name] = plugin

    def remove(self, name: str, plugin: object) -> None:
        from core.core_initializer import PLUGIN_REGISTRY

        if PLUGIN_REGISTRY.get(name) is plugin:
            PLUGIN_REGISTRY.pop(name, None)

    def taken_keys(self) -> set[str]:
        from core.recon import get_registered_recon_keys

        return set(get_registered_recon_keys())
