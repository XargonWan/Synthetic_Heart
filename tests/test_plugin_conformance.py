"""Static conformance checks for plugins/ (AGENTS.md operating rules).

The repository already contains a handful of known deviations. They are listed
explicitly below so this test fails on any NEW violation without demanding a
big-bang cleanup; shrink the allowlists as the deviations are fixed.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins"

# --- known, tracked deviations (remove entries as they are fixed) ----------
ENV_READS_ALLOWED: dict[str, int] = {
    "live_engines/gemini.py": 1,
    "soul_plugin/soul_plugin.py": 1,
    "event_plugin/event_plugin.py": 5,
    "radio_host/radio_host_plugin.py": 1,
    "mate_engine/mate_engine.py": 1,
    "agent_plugin/agent_plugin.py": 1,
    "rift_vessel/minecraft/minecraft.py": 1,
}
AIOMYSQL_IMPORT_ALLOWED = {
    "ai_diary/ai_diary.py",
    "blocklist/blocklist.py",
    "recent_chats/recent_chats.py",
}
DUPLICATE_REGISTRATIONS_ALLOWED: set[str] = set()
# Shared *_base modules are the sanctioned cross-package import; this one is not.
TOPLEVEL_CROSS_IMPORT_ALLOWED = {
    ("web_search_plugin/web_search_plugin.py", "plugins.web_search.search_engine"),
}


def _plugin_files() -> list[Path]:
    return sorted(p for p in PLUGINS_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(PLUGINS_DIR).as_posix()


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None


def test_no_print_or_basicconfig_in_plugins() -> None:
    offenders = []
    for path in _plugin_files():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name in {"print", "logging.basicConfig"}:
                    offenders.append(f"{_rel(path)}:{node.lineno} {name}")
    assert not offenders, "use core.logging_utils instead:\n" + "\n".join(offenders)


def test_env_reads_do_not_grow() -> None:
    counts: collections.Counter[str] = collections.Counter()
    for path in _plugin_files():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name == "os.getenv" or name.endswith("environ.get"):
                    counts[_rel(path)] += 1
            elif isinstance(node, ast.Subscript):
                if ast.unparse(node.value).endswith("os.environ"):
                    counts[_rel(path)] += 1
    grown = {f: n for f, n in counts.items() if n > ENV_READS_ALLOWED.get(f, 0)}
    assert not grown, (
        "tunables belong in the config registry (config_registry.get_var), not "
        f"os.environ: {grown}"
    )


def test_aiomysql_not_imported_by_new_plugins() -> None:
    offenders = []
    for path in _plugin_files():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "aiomysql" or n.startswith("aiomysql.") for n in names):
                if _rel(path) not in AIOMYSQL_IMPORT_ALLOWED:
                    offenders.append(_rel(path))
    assert not offenders, "use core.db.DictCursor / get_conn_ctx: " + ", ".join(
        sorted(set(offenders))
    )


def test_toplevel_cross_plugin_imports_are_shared_bases_only() -> None:
    offenders = []
    for path in _plugin_files():
        tree = _parse(path)
        if tree is None:
            continue
        own = _rel(path).split("/")[0].removesuffix(".py")
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            else:
                continue
            for module in modules:
                parts = module.split(".")
                if parts[0] != "plugins" or len(parts) < 2 or parts[1] == own:
                    continue
                if parts[1].endswith("_base"):
                    continue
                if (_rel(path), module) in TOPLEVEL_CROSS_IMPORT_ALLOWED:
                    continue
                offenders.append(f"{_rel(path)} imports {module}")
    assert not offenders, (
        "a plugin must stay removable: import other plugins lazily and guarded:\n"
        + "\n".join(offenders)
    )


def test_plugin_registration_keys_are_unique() -> None:
    registrations: dict[str, set[str]] = collections.defaultdict(set)
    for path in _plugin_files():
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and ast.unparse(node.func) == "register_plugin"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                registrations[node.args[0].value].add(_rel(path))
    duplicated = {
        k for k, files in registrations.items() if len(files) > 1
    } - DUPLICATE_REGISTRATIONS_ALLOWED
    assert not duplicated, f"registry keys registered by several modules: {duplicated}"
