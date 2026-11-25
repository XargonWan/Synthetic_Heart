"""Tests for ``core.extensions.resolver`` (platform milestone M1)."""

from __future__ import annotations

from core.extensions.manifest import (
    ExtensionDependency,
    InterfaceProvision,
    Manifest,
    Provides,
    Requires,
)
from core.extensions.resolver import (
    CORE_API_VERSION,
    WARNING,
    Resolution,
    check_install,
    resolve,
)


def ext(
    ext_id: str,
    *,
    version: str = "1.0.0",
    core_api: str = ">=0.1",
    needs: tuple[ExtensionDependency, ...] = (),
    interfaces: tuple[str, ...] = (),
    actions: tuple[str, ...] = (),
) -> Manifest:
    return Manifest(
        id=ext_id,
        name=ext_id,
        version=version,
        core_api=core_api,
        provides=Provides(
            interfaces=tuple(InterfaceProvision(id=i) for i in interfaces),
            actions=actions,
        ),
        requires=Requires(extensions=needs),
    )


def dep(
    ext_id: str, version: str = "", *, optional: bool = False
) -> ExtensionDependency:
    return ExtensionDependency(id=ext_id, version=version, optional=optional)


def pool(*manifests: Manifest) -> dict[str, Manifest]:
    return {m.id: m for m in manifests}


def codes(result: Resolution, ext_id: str) -> list[str]:
    return [i.code for i in result.excluded.get(ext_id, [])]


class TestOrdering:
    def test_empty(self) -> None:
        result = resolve({})
        assert result.order == [] and result.excluded == {}

    def test_dependencies_start_first(self) -> None:
        manifests = pool(
            ext("a/app", needs=(dep("a/lib"),)),
            ext("a/lib", needs=(dep("a/base"),)),
            ext("a/base"),
        )
        assert resolve(manifests).order == ["a/base", "a/lib", "a/app"]

    def test_order_is_deterministic(self) -> None:
        manifests = pool(ext("z/z"), ext("a/a"), ext("m/m"))
        assert resolve(manifests).order == ["a/a", "m/m", "z/z"]

    def test_present_optional_dependency_orders_first(self) -> None:
        manifests = pool(
            ext("a/app", needs=(dep("z/renderer", optional=True),)),
            ext("z/renderer"),
        )
        assert resolve(manifests).order == ["z/renderer", "a/app"]


class TestRequiredDependencies:
    def test_missing_required_dependency_excludes_only_that_extension(self) -> None:
        manifests = pool(ext("a/app", needs=(dep("a/missing"),)), ext("a/other"))
        result = resolve(manifests)
        assert result.order == ["a/other"]
        assert codes(result, "a/app") == ["missing_dependency"]

    def test_exclusion_cascades_to_dependants(self) -> None:
        manifests = pool(
            ext("a/top", needs=(dep("a/mid"),)),
            ext("a/mid", needs=(dep("a/missing"),)),
        )
        result = resolve(manifests)
        assert result.order == []
        assert codes(result, "a/mid") == ["missing_dependency"]
        assert codes(result, "a/top") == ["dependency_unavailable"]

    def test_version_constraint_is_enforced(self) -> None:
        manifests = pool(
            ext("a/app", needs=(dep("a/lib", ">=2"),)), ext("a/lib", version="1.5.0")
        )
        result = resolve(manifests)
        assert codes(result, "a/app") == ["version_conflict"]
        assert result.order == ["a/lib"]

    def test_version_constraint_satisfied(self) -> None:
        manifests = pool(
            ext("a/app", needs=(dep("a/lib", ">=1,<2"),)), ext("a/lib", version="1.5.0")
        )
        assert resolve(manifests).order == ["a/lib", "a/app"]


class TestOptionalDependencies:
    def test_missing_optional_dependency_is_not_a_problem(self) -> None:
        manifests = pool(ext("a/app", needs=(dep("a/nice", optional=True),)))
        result = resolve(manifests)
        assert result.order == ["a/app"] and result.issues == []

    def test_unsatisfied_optional_version_warns_but_starts(self) -> None:
        manifests = pool(
            ext("a/app", needs=(dep("a/lib", ">=3", optional=True),)),
            ext("a/lib", version="1.0.0"),
        )
        result = resolve(manifests)
        assert "a/app" in result.order and result.excluded == {}
        warning = result.issues_for("a/app")[0]
        assert warning.severity == WARNING
        assert warning.code == "optional_dependency_unsatisfied"

    def test_excluded_optional_dependency_does_not_exclude_the_dependant(self) -> None:
        manifests = pool(
            ext("a/app", needs=(dep("a/lib", optional=True),)),
            ext("a/lib", needs=(dep("a/missing"),)),
        )
        result = resolve(manifests)
        assert result.order == ["a/app"]
        assert "a/lib" in result.excluded


class TestCycles:
    def test_required_cycle_excludes_every_member(self) -> None:
        manifests = pool(
            ext("a/one", needs=(dep("a/two"),)),
            ext("a/two", needs=(dep("a/three"),)),
            ext("a/three", needs=(dep("a/one"),)),
            ext("a/free"),
        )
        result = resolve(manifests)
        assert result.order == ["a/free"]
        for member in ("a/one", "a/two", "a/three"):
            assert "dependency_cycle" in codes(result, member)

    def test_dependant_of_a_cycle_is_excluded_too(self) -> None:
        manifests = pool(
            ext("a/one", needs=(dep("a/two"),)),
            ext("a/two", needs=(dep("a/one"),)),
            ext("a/user", needs=(dep("a/one"),)),
        )
        result = resolve(manifests)
        assert result.order == []
        assert codes(result, "a/user") == ["dependency_unavailable"]

    def test_self_dependency_is_a_cycle(self) -> None:
        result = resolve(pool(ext("a/me", needs=(dep("a/me"),))))
        assert codes(result, "a/me") == ["dependency_cycle"]

    def test_optional_edge_closing_a_cycle_is_ignored(self) -> None:
        manifests = pool(
            ext("a/one", needs=(dep("a/two"),)),
            ext("a/two", needs=(dep("a/one", optional=True),)),
        )
        result = resolve(manifests)
        assert result.excluded == {}
        assert result.order == ["a/two", "a/one"]


class TestCoreApiCompatibility:
    def test_incompatible_core_api_is_excluded(self) -> None:
        manifests = pool(ext("a/old", core_api=">=2"), ext("a/ok", core_api=">=0.1,<1"))
        result = resolve(manifests)
        assert result.order == ["a/ok"]
        assert codes(result, "a/old") == ["incompatible_core_api"]

    def test_core_version_is_a_parameter(self) -> None:
        manifests = pool(ext("a/new", core_api=">=2"))
        assert resolve(manifests, core_api_version="2.1.0").order == ["a/new"]
        assert resolve(manifests).order == []
        assert CORE_API_VERSION == "0.1.0"

    def test_incompatible_extension_cascades(self) -> None:
        manifests = pool(
            ext("a/lib", core_api=">=9"), ext("a/app", needs=(dep("a/lib"),))
        )
        assert resolve(manifests).order == []


class TestProvidesConflicts:
    def test_two_native_extensions_with_the_same_interface_are_both_excluded(
        self,
    ) -> None:
        manifests = pool(
            ext("a/one", interfaces=("chat",)), ext("a/two", interfaces=("chat",))
        )
        result = resolve(manifests)
        assert result.order == []
        assert codes(result, "a/one") == ["provides_conflict"]
        assert codes(result, "a/two") == ["provides_conflict"]

    def test_action_names_conflict_too(self) -> None:
        manifests = pool(
            ext("a/one", actions=("do_it",)), ext("a/two", actions=("do_it",))
        )
        assert resolve(manifests).order == []


class TestCheckInstall:
    def test_safe_install_returns_no_problems(self) -> None:
        installed = pool(ext("a/lib"))
        candidate = ext("a/app", needs=(dep("a/lib"),))
        assert check_install(candidate, installed) == []

    def test_candidate_with_missing_dependency_is_refused(self) -> None:
        candidate = ext("a/app", needs=(dep("a/lib"),))
        problems = check_install(candidate, {})
        assert [p.code for p in problems] == ["missing_dependency"]
        assert problems[0].extension_id == "a/app"

    def test_install_that_would_break_an_installed_extension_is_refused(self) -> None:
        installed = pool(ext("a/one", interfaces=("chat",)))
        problems = check_install(ext("a/two", interfaces=("chat",)), installed)
        assert {p.extension_id for p in problems} == {"a/one", "a/two"}

    def test_upgrade_replaces_the_installed_version(self) -> None:
        installed = pool(
            ext("a/lib", version="1.0.0"),
            ext("a/app", needs=(dep("a/lib", "<2"),)),
        )
        upgrade = ext("a/lib", version="2.0.0")
        problems = check_install(upgrade, installed)
        assert [(p.extension_id, p.code) for p in problems] == [
            ("a/app", "version_conflict")
        ]

    def test_problems_that_already_existed_are_not_blamed_on_the_candidate(
        self,
    ) -> None:
        installed = pool(ext("a/broken", needs=(dep("a/missing"),)))
        assert check_install(ext("a/new"), installed) == []
