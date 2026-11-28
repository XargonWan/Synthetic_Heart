"""The first extension API surface (platform milestone M2).

``register_core_api(gateway, ports)`` adds the methods an extension can call:

=================  ============  ===================================================
Method             Capability    What it does
=================  ============  ===================================================
``log.write``      ``log``       a log line, tagged with the extension id
``config.get``     ``config``    one of the extension's own declared config keys
``config.all``     ``config``    all of them
``storage.get``    ``storage``   read a key of the extension's own namespace
``storage.set``    ``storage``   write a key (size and quota limited)
``storage.delete`` ``storage``   delete a key
``storage.keys``   ``storage``   list keys with a prefix
``stats.incr``     ``stats``     a counter, series prefixed ``ext.<id>.``
``stats.observe``  ``stats``     a timing, same prefix
``stats.gauge``    ``stats``     a gauge, same prefix
``events.publish`` ``events``    publish on ``ext.<id>.<topic>``
``events.subscribe`` ``events``  subscribe to own, ``core.`` or dependency topics
``events.unsubscribe`` ``events`` the reverse
=================  ============  ===================================================

Namespacing is enforced here, **not** by the extension: the caller's id comes from
the session, so no parameter can name another extension's data, series or topic.
"""

from __future__ import annotations

import json
import re
from typing import Any

from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.gateway import (
    ApiMethod,
    CallContext,
    Gateway,
    Param,
    extension_slug,
)
from core.extensions.ports import ApiPorts, QuotaExceeded, StorageLimits

LOG_LEVELS = ("debug", "info", "warning", "error")
MAX_LOG_MESSAGE = 4000

_KEY_RE = re.compile(r"[A-Za-z0-9_.:/-]+")
_CONFIG_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_STAT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_LABEL_KEY_RE = re.compile(r"[a-z][a-z0-9_]*")
_TOPIC_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_MAX_LABELS = 4
_MAX_LABEL_VALUE = 64
_MAX_STAT_NAME = 100
_MAX_TOPIC = 100


def stats_series(extension_id: str, name: str) -> str:
    """The series an extension's statistic is recorded under."""
    return f"ext.{extension_slug(extension_id)}.{name}"


def own_topic_prefix(extension_id: str) -> str:
    return f"ext.{extension_slug(extension_id)}."


def _labels(raw: dict[str, Any]) -> dict[str, str]:
    if len(raw) > _MAX_LABELS:
        raise RpcError(ErrorCode.INVALID_PARAMS, f"at most {_MAX_LABELS} labels")
    labels: dict[str, str] = {}
    for key, value in raw.items():
        if not _LABEL_KEY_RE.fullmatch(key) or len(key) > 30:
            raise RpcError(ErrorCode.INVALID_PARAMS, f"invalid label name {key!r}")
        if not isinstance(value, str) or len(value) > _MAX_LABEL_VALUE:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"label {key!r} must be a string of at most {_MAX_LABEL_VALUE} characters",
            )
        labels[key] = value
    return labels


def register_core_api(
    gateway: Gateway,
    ports: ApiPorts,
    *,
    storage_limits: StorageLimits | None = None,
) -> None:
    """Register the extension API methods on ``gateway``."""
    limits = storage_limits or StorageLimits()

    # ------------------------------------------------------------------ log

    async def log_write(ctx: CallContext, args: dict[str, Any]) -> None:
        ports.log.write(ctx.extension_id, args["level"], args["message"])

    # --------------------------------------------------------------- config

    def declared(ctx: CallContext, key: str) -> Any:
        for item in ctx.manifest.config:
            if item.key == key:
                return item
        raise RpcError(ErrorCode.INVALID_PARAMS, f"unknown config key: {key}")

    async def config_get(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        field = declared(ctx, args["key"])
        return {"value": ports.config.get(ctx.extension_id, field)}

    async def config_all(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "values": {
                f.key: ports.config.get(ctx.extension_id, f) for f in ctx.manifest.config
            }
        }

    # -------------------------------------------------------------- storage

    async def storage_get(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        text = await ports.storage.get(ctx.extension_id, args["key"])
        if text is None:
            return {"found": False, "value": None}
        return {"found": True, "value": json.loads(text)}

    async def storage_set(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        try:
            text = json.dumps(args["value"], separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise RpcError(ErrorCode.INVALID_PARAMS, "value is not valid JSON")
        if len(text.encode("utf-8")) > limits.max_value_bytes:
            raise RpcError(
                ErrorCode.QUOTA_EXCEEDED,
                f"value is larger than {limits.max_value_bytes} bytes",
            )
        try:
            await ports.storage.set(ctx.extension_id, args["key"], text)
        except QuotaExceeded as exc:
            raise RpcError(ErrorCode.QUOTA_EXCEEDED, str(exc))
        return {"stored": True}

    async def storage_delete(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        return {"deleted": await ports.storage.delete(ctx.extension_id, args["key"])}

    async def storage_keys(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        limit = min(args["limit"], limits.max_list_results)
        keys = await ports.storage.keys(ctx.extension_id, args["prefix"], limit)
        return {"keys": keys}

    # ---------------------------------------------------------------- stats

    async def stats_incr(ctx: CallContext, args: dict[str, Any]) -> None:
        ports.stats.incr(
            stats_series(ctx.extension_id, args["name"]), args["n"], _labels(args["labels"])
        )

    async def stats_observe(ctx: CallContext, args: dict[str, Any]) -> None:
        ports.stats.observe(
            stats_series(ctx.extension_id, args["name"]),
            float(args["seconds"]),
            _labels(args["labels"]),
        )

    async def stats_gauge(ctx: CallContext, args: dict[str, Any]) -> None:
        ports.stats.gauge(
            stats_series(ctx.extension_id, args["name"]), float(args["value"])
        )

    # --------------------------------------------------------------- events

    async def events_publish(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        topic = own_topic_prefix(ctx.extension_id) + args["topic"]
        return {"delivered": gateway.events.publish(topic, args["payload"])}

    def subscribable(ctx: CallContext, topic: str) -> bool:
        if topic.startswith(own_topic_prefix(ctx.extension_id)) or topic.startswith("core."):
            return True
        return any(
            topic.startswith(own_topic_prefix(dep.id))
            for dep in ctx.manifest.requires.extensions
        )

    async def events_subscribe(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        topic = args["topic"]
        if not subscribable(ctx, topic):
            gateway.record(ctx.extension_id, "events.subscribe", "denied", topic)
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                "topic is not one of yours, a core topic or a dependency's",
            )
        gateway.events.subscribe(ctx.session, topic)
        return {"subscribed": topic}

    async def events_unsubscribe(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        gateway.events.unsubscribe(ctx.session, args["topic"])
        return {"unsubscribed": args["topic"]}

    key = Param("key", max_length=limits.max_key_length, pattern=_KEY_RE)
    stat_name = Param("name", max_length=_MAX_STAT_NAME, pattern=_STAT_NAME_RE)
    labels = Param("labels", "object", required=False, default={}, max_length=_MAX_LABELS)
    topic_name = Param("topic", max_length=_MAX_TOPIC, pattern=_TOPIC_RE)

    for method in (
        ApiMethod(
            "log.write",
            "log",
            (
                Param("level", choices=LOG_LEVELS),
                Param("message", max_length=MAX_LOG_MESSAGE),
            ),
            log_write,
        ),
        ApiMethod(
            "config.get",
            "config",
            (Param("key", max_length=100, pattern=_CONFIG_KEY_RE),),
            config_get,
        ),
        ApiMethod("config.all", "config", (), config_all),
        ApiMethod("storage.get", "storage", (key,), storage_get),
        ApiMethod("storage.set", "storage", (key, Param("value", "json")), storage_set),
        ApiMethod("storage.delete", "storage", (key,), storage_delete),
        ApiMethod(
            "storage.keys",
            "storage",
            (
                Param(
                    "prefix",
                    required=False,
                    default="",
                    max_length=limits.max_key_length,
                    pattern=re.compile(r"[A-Za-z0-9_.:/-]*"),
                ),
                Param(
                    "limit",
                    "int",
                    required=False,
                    default=limits.max_list_results,
                    minimum=1,
                    maximum=limits.max_list_results,
                ),
            ),
            storage_keys,
        ),
        ApiMethod(
            "stats.incr",
            "stats",
            (
                stat_name,
                Param("n", "int", required=False, default=1, minimum=-1_000_000, maximum=1_000_000),
                labels,
            ),
            stats_incr,
        ),
        ApiMethod(
            "stats.observe",
            "stats",
            (stat_name, Param("seconds", "float", minimum=0, maximum=86_400 * 365), labels),
            stats_observe,
        ),
        ApiMethod(
            "stats.gauge",
            "stats",
            (stat_name, Param("value", "float", minimum=-1e15, maximum=1e15)),
            stats_gauge,
        ),
        ApiMethod(
            "events.publish",
            "events",
            (topic_name, Param("payload", "json", required=False)),
            events_publish,
        ),
        ApiMethod("events.subscribe", "events", (topic_name,), events_subscribe),
        ApiMethod("events.unsubscribe", "events", (topic_name,), events_unsubscribe),
    ):
        gateway.register(method)
