"""Generate ``docs/plugin_platform/api_matrix.rst``.

Read-only static analysis (``ast``) of ``plugins/``, ``interface/`` and
``engines/``: for every extension candidate it lists the ``core`` symbols the
code imports or calls through a module alias. The result sizes the first public
extension API (see ``docs/plugin_platform/api_inventory.rst``).

Usage::

    uv run python scripts/plugin_platform_api_matrix.py          # rewrite the page
    uv run python scripts/plugin_platform_api_matrix.py --check  # exit 1 if stale

Limits: the analysis is static. It sees ``import`` statements and attribute
access on a module alias (``mod.func``). It does not see dynamic imports
(``importlib``), ``getattr`` or objects obtained indirectly.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "plugin_platform" / "api_matrix.rst"
SCAN_DIRS = ("plugins", "interface", "engines")
SKIP_PARTS = {"__pycache__", "node_modules", "searxng-runtime", ".venv", "mineflayer"}

# core module -> proposed public API namespace (see api_inventory.rst).
# Modules not listed fall under "unmapped" and need triage.
NAMESPACE_OF: dict[str, str] = {
    "logging_utils": "log",
    "cortex_api_logger": "log",
    "llm_failure_log": "log",
    "notifier": "log",
    "config_manager": "config",
    "config": "config",
    "variables_engine": "config",
    "time_zone_utils": "util",
    "text_utils": "util",
    "json_utils": "util",
    "calendar_utils": "util",
    "external_calendars": "util",
    "beat_utils": "util",
    "user_utils": "util",
    "media_url_utils": "util",
    "media_extract": "util",
    "app_paths": "files",
    "outbound_file_utils": "files",
    "url_safety": "files",
    "package_installer": "files",
    "model_manager": "files",
    "db": "storage",
    "core_initializer": "registration",
    "plugin_base": "registration",
    "ai_plugin_base": "registration",
    "plugin_instance": "registration",
    "validation_registry": "registration",
    "message_registry": "registration",
    "command_registry": "registration",
    "interfaces_registry": "registration",
    "optional_plugins": "registration",
    "capability_drops": "registration",
    "action_schema_converter": "registration",
    "action_parser": "registration",
    "interface_path_utils": "interfaces",
    "interface_paths": "interfaces",
    "interface_aliases": "interfaces",
    "message_queue": "interfaces",
    "chat_context_manager": "interfaces",
    "chat_history_cache": "interfaces",
    "history_types": "interfaces",
    "transport_layer": "interfaces",
    "message_sender": "interfaces",
    "message_chain": "interfaces",
    "mention_utils": "interfaces",
    "reaction_handler": "interfaces",
    "peer_policy": "interfaces",
    "chat_attention": "interfaces",
    "chat_update_checker": "interfaces",
    "auto_response": "interfaces",
    "response_proxy": "interfaces",
    "bot_ward": "interfaces",
    "delivery_guard": "interfaces",
    "voice_reply": "interfaces",
    "session_meta": "interfaces",
    "call_manager": "calls",
    "call_audio": "calls",
    "call_video": "calls",
    "call_ack_cache": "calls",
    "animation_handler": "karada",
    "karada_transport": "karada",
    "animation_uploads": "karada",
    "synth_avatar": "karada",
    "facial_expression_parser": "karada",
    "persona_manager": "karada",
    "stats_registry": "stats",
    "vessel_stats": "stats",
    "cortex_registry": "cortex",
    "prompt_engine": "cortex",
    "prompt_renderers": "cortex",
    "prompt_request": "cortex",
    "multimodal_attachment": "cortex",
    "genai_client_utils": "cortex",
    "external_endpoints": "cortex",
    "agent_core": "agents",
    "agent_router": "agents",
    "tool_registry": "agents",
    "live_tool_registry": "agents",
    "live_tool_executor": "agents",
    "live_tool_adapters": "agents",
    "vox_registry": "engines",
    "auris_registry": "engines",
    "iris_registry": "engines",
    "media_dispatcher": "engines",
    "live_session_manager": "engines",
    "soul": "soul",
    "growth_state": "soul",
    "synth_core_memory": "soul",
    "vessel_beat": "vessel",
    "vessel_focus": "vessel",
    "vessel_registry": "vessel",
    "vessel_session_manager": "vessel",
    "vessel_goal_debrief": "vessel",
    "vessel_diary_compactor": "vessel",
    "webui": "ui",
    "webui_auth": "ui",
}

# Symbols that are not an API at all but a reach into Core internals.
REACH_IN: dict[str, str] = {
    "PLUGIN_REGISTRY": "direct access to the plugin registry dict",
    "INTERFACE_REGISTRY": "direct access to the interface registry dict",
    "ACTION_REGISTRY": "direct access to the action registry dict",
    "core_initializer": "the CoreInitializer singleton itself",
    "synth_webui_interface": "the live WebUI interface object",
    "LiveSessionManager": "a Core class instantiated by the extension",
}


def namespace_of(module: str) -> str:
    """Return the proposed API namespace for a dotted ``core.*`` module."""
    parts = module.split(".")
    if len(parts) < 2:
        return "unmapped"
    return NAMESPACE_OF.get(parts[1], "unmapped")


def unit_of(path: Path) -> str:
    """Return the extension-candidate name for a source file."""
    rel = path.relative_to(ROOT).parts
    top = rel[0]
    if top == "engines":
        return "/".join(rel[:2]) if len(rel) > 2 else f"engines/{Path(rel[1]).stem}"
    if len(rel) == 2:
        return f"{top}/{Path(rel[1]).stem}"
    return f"{top}/{rel[1]}"


def analyse_file(path: Path) -> dict[str, set[str]]:
    """Return ``{core_module: {symbols}}`` used by one source file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return {}
    used: dict[str, set[str]] = defaultdict(set)
    alias_to_module: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "core":
                for alias in node.names:
                    alias_to_module[alias.asname or alias.name] = f"core.{alias.name}"
                    used[f"core.{alias.name}"].add("<module>")
            elif node.module.startswith("core."):
                for alias in node.names:
                    used[node.module].add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("core."):
                    used[alias.name].add("<module>")
                    if alias.asname:
                        alias_to_module[alias.asname] = alias.name
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in alias_to_module
        ):
            used[alias_to_module[node.value.id]].add(node.attr)
    for module, symbols in used.items():
        if len(symbols) > 1:
            symbols.discard("<module>")
    return dict(used)


def collect() -> tuple[dict[str, dict[str, set[str]]], dict[str, int], dict[str, int]]:
    """Scan the tree. Return per-unit usage, file counts and core-importing counts."""
    units: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    files: dict[str, int] = defaultdict(int)
    importing: dict[str, int] = defaultdict(int)
    for top in SCAN_DIRS:
        for path in sorted((ROOT / top).rglob("*.py")):
            if SKIP_PARTS & set(path.parts):
                continue
            unit = unit_of(path)
            files[unit] += 1
            used = analyse_file(path)
            if used:
                importing[unit] += 1
            for module, symbols in used.items():
                units[unit][module] |= symbols
    return units, files, importing


def render(
    units: dict[str, dict[str, set[str]]],
    files: dict[str, int],
    importing: dict[str, int],
) -> str:
    """Render the RST page."""
    names = sorted(files)
    total_files = sum(files.values())
    total_importing = sum(importing.values())

    ns_units: dict[str, set[str]] = defaultdict(set)
    ns_symbols: dict[str, set[str]] = defaultdict(set)
    sym_units: dict[str, set[str]] = defaultdict(set)
    for unit, modules in units.items():
        for module, symbols in modules.items():
            ns = namespace_of(module)
            ns_units[ns].add(unit)
            for symbol in symbols:
                ns_symbols[ns].add(f"{module}::{symbol}")
                sym_units[f"{module}::{symbol}"].add(unit)

    out: list[str] = []
    add = out.append
    add(
        ".. This page is generated by scripts/plugin_platform_api_matrix.py. Do not edit."
    )
    add("")
    add("Function-Level API Matrix")
    add("=========================")
    add("")
    add(
        "Which ``core`` symbols each extension candidate imports or calls. It sizes the "
        "first public API (:doc:`api_inventory`). Generated by "
        "``scripts/plugin_platform_api_matrix.py``; regenerate with "
        "``uv run python scripts/plugin_platform_api_matrix.py``."
    )
    add("")
    add(".. note::")
    add("")
    add(
        "   The analysis is **static** (``ast``). It sees ``import`` statements and "
        "attribute access on a module alias. It does not see dynamic imports, "
        "``getattr`` or objects reached indirectly, so real usage can be larger. "
        "Namespaces below are the *proposed* API namespaces, not existing code."
    )
    add("")
    add(
        f"Scanned **{total_files}** Python files in ``{', '.join(SCAN_DIRS)}``, grouped into "
        f"**{len(names)}** units; **{total_importing}** files import ``core``."
    )
    add("")

    add("Proposed API namespaces: how much each one must cover")
    add("-------------------------------------------------------")
    add("")
    add(".. list-table::")
    add("   :header-rows: 1")
    add("   :widths: 20 20 20 40")
    add("")
    add("   * - Namespace")
    add("     - Units using it")
    add("     - Distinct symbols")
    add("     - Meaning")
    meaning = {
        "log": "logging and notifications",
        "config": "configuration and exposed variables",
        "storage": "database access (``core.db``), to become the ``storage`` API",
        "registration": "registries, base classes, command/validation/message registration",
        "interfaces": "interface paths, message queue, chat history, transport",
        "karada": "state server, avatar, persona animation",
        "stats": "statistics",
        "cortex": "cortex registry, prompt engine, endpoints",
        "agents": "agent runtime and tool registries",
        "engines": "Vox/Auris/Iris/Live registries and sessions",
        "soul": "SOUL models and strategies",
        "vessel": "Rift Vessel API (Core; used by adapters)",
        "calls": "voice/video call stack",
        "files": "paths, safe file output, model and package installation",
        "ui": "WebUI objects",
        "util": "small helpers (time, text, JSON, calendar)",
        "unmapped": "not yet assigned: triage needed",
    }
    for ns in sorted(ns_units, key=lambda n: (-len(ns_units[n]), n)):
        add(f"   * - ``{ns}``")
        add(f"     - {len(ns_units[ns])}")
        add(f"     - {len(ns_symbols[ns])}")
        add(f"     - {meaning.get(ns, '')}")
    add("")

    add("Most used symbols")
    add("-----------------")
    add("")
    add("The 30 symbols used by the most units. These belong in the first API surface.")
    add("")
    add(".. list-table::")
    add("   :header-rows: 1")
    add("   :widths: 12 58 30")
    add("")
    add("   * - Units")
    add("     - Symbol")
    add("     - Namespace")
    ranked = sorted(sym_units.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:30]
    for sym, users in ranked:
        module = sym.split("::", 1)[0]
        add(f"   * - {len(users)}")
        add(f"     - ``{sym}``")
        add(f"     - ``{namespace_of(module)}``")
    add("")

    add("Reaching into Core internals")
    add("----------------------------")
    add("")
    add(
        "These are not API calls but direct access to Core objects. Under a process "
        "boundary each one needs a proper API or must disappear."
    )
    add("")
    add(".. list-table::")
    add("   :header-rows: 1")
    add("   :widths: 40 10 50")
    add("")
    add("   * - Symbol")
    add("     - Units")
    add("     - Units using it")
    reach = []
    for sym, users in sym_units.items():
        name = sym.split("::", 1)[1]
        if name in REACH_IN or name.startswith("_"):
            reach.append((sym, users))
    for sym, users in sorted(reach, key=lambda kv: (-len(kv[1]), kv[0])):
        name = sym.split("::", 1)[1]
        why = REACH_IN.get(name, "private (underscore) symbol")
        add(f"   * - ``{sym}`` ({why})")
        add(f"     - {len(users)}")
        add(f"     - {', '.join(f'``{u}``' for u in sorted(users))}")
    add("")

    add("Per-unit summary")
    add("----------------")
    add("")
    add(".. list-table::")
    add("   :header-rows: 1")
    add("   :widths: 40 12 16 16 16")
    add("")
    add("   * - Unit")
    add("     - Files")
    add("     - Import core")
    add("     - Core modules")
    add("     - Symbols")
    for unit in names:
        modules = units.get(unit, {})
        add(f"   * - ``{unit}``")
        add(f"     - {files[unit]}")
        add(f"     - {importing.get(unit, 0)}")
        add(f"     - {len(modules)}")
        add(f"     - {sum(len(s) for s in modules.values())}")
    add("")

    add("Per-unit detail")
    add("---------------")
    add("")
    for unit in names:
        modules = units.get(unit, {})
        add(f"``{unit}``")
        add("~" * (len(unit) + 4))
        add("")
        if not modules:
            add("No ``core`` import found.")
            add("")
            continue
        add(".. code-block:: text")
        add("")
        for module in sorted(modules):
            symbols = ", ".join(sorted(modules[module]))
            add(f"   [{namespace_of(module)}] {module}: {symbols}")
        add("")
    return "\n".join(out) + "\n"


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="fail if the page is stale"
    )
    args = parser.parse_args()
    units, files, importing = collect()
    text = render(units, files, importing)
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            sys.stderr.write(f"{OUTPUT} is stale; regenerate it.\n")
            return 1
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    sys.stdout.write(f"wrote {OUTPUT.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
