"""Dependency and compatibility resolution for extensions (milestone M1).

Given a set of manifests, :func:`resolve` decides which of them can start, in
which order, and why the others cannot. It never raises for a bad extension: a
problem **excludes that extension** (and whatever requires it) and is reported,
so a broken optional extension cannot take the Core or its neighbours down.

Rules:

* ``compat.core_api`` must accept :data:`CORE_API_VERSION`.
* A **required** dependency must be present, not excluded, and satisfy the
  version specifier. Exclusion cascades to its dependants.
* **Optional** dependencies change ordering and behaviour, never installability.
* A cycle through required dependencies excludes every member of the cycle. An
  optional edge that would close a cycle is ignored.
* Two extensions providing the same interface id or action name conflict: **both**
  are excluded and the user decides which one to keep. (A clash with an interface
  that is still loaded the old, in-process way is caught when the extension
  registers it: see ``core.extensions.interfaces``.)

Like :mod:`core.extensions.manifest` this module imports nothing from ``core``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from core.extensions.manifest import Manifest

#: Version of the public extension API. The API does not exist yet (milestone M2
#: introduces it), so this is a pre-release marker. Manifests state the range
#: they work with in ``compat.core_api``.
CORE_API_VERSION = "0.1.0"

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    """One finding about one extension."""

    extension_id: str
    code: str
    message: str
    severity: str = ERROR


@dataclass
class Resolution:
    """Outcome of :func:`resolve`."""

    #: Extension ids that can start, dependencies first.
    order: list[str] = field(default_factory=list)
    #: Excluded extension ids with the reasons (error issues only).
    excluded: dict[str, list[Issue]] = field(default_factory=dict)
    #: Every finding, including warnings.
    issues: list[Issue] = field(default_factory=list)

    def issues_for(self, extension_id: str) -> list[Issue]:
        """Findings about one extension."""
        return [i for i in self.issues if i.extension_id == extension_id]


class _State:
    def __init__(self, manifests: Mapping[str, Manifest]) -> None:
        self.manifests = manifests
        self.excluded: dict[str, list[Issue]] = {}
        self.issues: list[Issue] = []

    def alive(self) -> list[str]:
        return sorted(i for i in self.manifests if i not in self.excluded)

    def exclude(self, ext_id: str, code: str, message: str) -> bool:
        """Exclude an extension. Returns True if it was newly excluded."""
        issue = Issue(ext_id, code, message)
        self.issues.append(issue)
        if ext_id in self.excluded:
            self.excluded[ext_id].append(issue)
            return False
        self.excluded[ext_id] = [issue]
        return True

    def warn(self, ext_id: str, code: str, message: str) -> None:
        self.issues.append(Issue(ext_id, code, message, WARNING))


def _check_core_api(state: _State, core_version: Version) -> None:
    for ext_id in state.alive():
        spec = state.manifests[ext_id].core_api
        if not spec:
            continue
        if not SpecifierSet(spec).contains(core_version, prereleases=True):
            state.exclude(
                ext_id,
                "incompatible_core_api",
                f"requires Core API {spec!r}, this Core provides {core_version}",
            )


def _check_provides_conflicts(state: _State) -> None:
    for kind, getter in (
        ("interface", lambda m: m.interface_ids),
        ("action", lambda m: m.provides.actions),
    ):
        owners: dict[str, list[str]] = {}
        for ext_id in state.alive():
            for name in getter(state.manifests[ext_id]):
                owners.setdefault(name, []).append(ext_id)
        for name, ids in sorted(owners.items()):
            if len(ids) < 2:
                continue
            others = ", ".join(sorted(ids))
            for ext_id in ids:
                state.exclude(
                    ext_id,
                    "provides_conflict",
                    f"{kind} {name!r} is also provided by: {others}",
                )


def _check_dependencies(state: _State) -> bool:
    """Apply dependency rules once. Returns True if anything was excluded."""
    changed = False
    for ext_id in state.alive():
        for dep in state.manifests[ext_id].requires.extensions:
            target = state.manifests.get(dep.id)
            if target is None:
                if not dep.optional:
                    changed |= state.exclude(
                        ext_id,
                        "missing_dependency",
                        f"requires {dep.id!r}, which is not installed",
                    )
                continue
            if dep.id in state.excluded:
                if not dep.optional:
                    changed |= state.exclude(
                        ext_id,
                        "dependency_unavailable",
                        f"requires {dep.id!r}, which cannot start",
                    )
                continue
            if dep.version and not SpecifierSet(dep.version).contains(
                Version(target.version), prereleases=True
            ):
                message = (
                    f"requires {dep.id!r} {dep.version!r}, "
                    f"but version {target.version} is installed"
                )
                if dep.optional:
                    state.warn(ext_id, "optional_dependency_unsatisfied", message)
                else:
                    changed |= state.exclude(ext_id, "version_conflict", message)
    return changed


def _required_cycles(state: _State) -> list[list[str]]:
    """Strongly connected components with a cycle, over required edges."""
    alive = set(state.alive())
    edges: dict[str, list[str]] = {
        i: sorted(
            d.id
            for d in state.manifests[i].requires.extensions
            if not d.optional and d.id in alive
        )
        for i in alive
    }
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []
    counter = 0

    def visit(node: str) -> None:
        nonlocal counter
        index[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in edges[node]:
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1 or node in edges[node]:
                cycles.append(sorted(component))

    for node in sorted(alive):
        if node not in index:
            visit(node)
    return cycles


def _order(state: _State) -> list[str]:
    """Dependencies-first order. Optional edges that would close a cycle are skipped."""
    alive = set(state.alive())
    done: set[str] = set()
    visiting: set[str] = set()
    result: list[str] = []

    def visit(node: str) -> None:
        visiting.add(node)
        deps = state.manifests[node].requires.extensions
        required = sorted(d.id for d in deps if not d.optional and d.id in alive)
        optional = sorted(d.id for d in deps if d.optional and d.id in alive)
        for dep_id in required + optional:
            if dep_id in done:
                continue
            if dep_id in visiting:
                continue  # only optional edges can reach here: cycles were excluded
            visit(dep_id)
        visiting.discard(node)
        done.add(node)
        result.append(node)

    for node in sorted(alive):
        if node not in done:
            visit(node)
    return result


def resolve(
    manifests: Mapping[str, Manifest],
    *,
    core_api_version: str = CORE_API_VERSION,
) -> Resolution:
    """Decide which extensions can start and in which order."""
    state = _State(manifests)
    _check_core_api(state, Version(core_api_version))
    _check_provides_conflicts(state)

    while True:
        changed = _check_dependencies(state)
        for cycle in _required_cycles(state):
            members = ", ".join(cycle)
            for ext_id in cycle:
                changed |= state.exclude(
                    ext_id,
                    "dependency_cycle",
                    f"takes part in a dependency cycle: {members}",
                )
        if not changed:
            break

    return Resolution(
        order=_order(state),
        excluded={k: list(v) for k, v in state.excluded.items()},
        issues=list(state.issues),
    )


def check_install(
    candidate: Manifest,
    installed: Mapping[str, Manifest],
    *,
    core_api_version: str = CORE_API_VERSION,
) -> list[Issue]:
    """Return the problems that installing ``candidate`` would cause.

    Empty means the install is safe: nothing that worked before stops working and
    the candidate itself can start. This is run *before* anything is changed.
    """
    before = resolve(installed, core_api_version=core_api_version)
    combined = dict(installed)
    combined[candidate.id] = candidate
    after = resolve(combined, core_api_version=core_api_version)
    newly_excluded = set(after.excluded) - set(before.excluded)
    problems: list[Issue] = []
    for ext_id in sorted(newly_excluded):
        problems.extend(after.excluded[ext_id])
    return problems
