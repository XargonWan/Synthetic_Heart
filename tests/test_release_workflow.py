"""The release workflow's own shell, run for real.

The Windows installer cannot be built on this machine's CI budget, and a workflow
cannot be run here at all, so the parts of it that *can* be executed are executed:
the shell steps that decide which image tags are pushed and whether the file about
to be published is a whole installer. They are extracted from the workflow file and
run as written, so a retyped copy cannot drift away from what CI runs. The version
itself comes from ``scripts/version.py``, tested in ``test_release_cycle.py``.

Also asserted structurally: the release job consumes exactly the artifact name the
installer job produces. That mismatch is the one that would silently publish nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-release.yml"
)
BASH = shutil.which("bash")


def _job(name: str) -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert name in workflow["jobs"], f"the workflow has no {name} job"
    return workflow["jobs"][name]


def _step(job: str, name: str) -> dict:
    for step in _job(job)["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"the {job} job has no step called {name!r}")


def _interpolate(script: str, values: dict[str, str]) -> str:
    """Substitute GitHub's expressions the way a runner does before bash sees them."""

    def replace(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        assert expression in values, (
            f"the step uses {expression}, which this test does not supply"
        )
        return values[expression]

    return re.sub(r"\$\{\{(.+?)\}\}", replace, script)


def _run_bash(
    script: str,
    cwd: Path,
    extra_env: dict[str, str],
    values: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a workflow shell step as bash, on a runner's Ubuntu, with its expressions filled in."""
    env = {**os.environ, "GITHUB_OUTPUT": str(cwd / "out.txt"), **extra_env}
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _interpolate(script, values or {})],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


pytestmark = pytest.mark.skipif(
    BASH is None, reason="the workflow steps are bash, and there is no bash here"
)


def _run_tags_step(
    tmp_path: Path, version: str, ref_name: str, event: str = "push"
) -> str:
    script = _step("manifest", "Compute image tags")["run"]
    result = _run_bash(
        script,
        tmp_path,
        {
            "IMAGE_NAME": "me/synth",
            "VERSION": version,
            "REF_NAME": ref_name,
            "PR_NUMBER": "7",
            "GITHUB_EVENT_NAME": event,
        },
    )
    assert result.returncode == 0, result.stderr
    return (tmp_path / "out.txt").read_text().strip().removeprefix("tags=")


# --------------------------------------------------------------------------- tags


def test_main_publishes_the_plain_version_and_latest(tmp_path: Path) -> None:
    assert _run_tags_step(tmp_path, "1.2.3", "main") == "me/synth:1.2.3,me/synth:latest"


def test_develop_publishes_its_version_and_latest_develop(tmp_path: Path) -> None:
    tags = _run_tags_step(tmp_path, "1.3.0-develop", "develop")
    assert tags == "me/synth:1.3.0-develop,me/synth:latest-develop"


def test_a_branch_never_gets_latest(tmp_path: Path) -> None:
    tags = _run_tags_step(tmp_path, "1.3.0-feat-x", "feat/x")
    assert tags == "me/synth:1.3.0-feat-x"


def test_a_pull_request_is_tagged_apart(tmp_path: Path) -> None:
    tags = _run_tags_step(tmp_path, "1.3.0-feat-x", "7/merge", "pull_request")
    assert tags == "me/synth:pr7-1.3.0-feat-x"


# --------------------------------------------------------------------------- publish


def _publish_step_script() -> str:
    return _step("release", "Check it is a complete installer before publishing it")[
        "run"
    ]


def _fake_installer(tmp_path: Path, size: int) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    exe = dist / "SyntH-Setup-1.0.16.exe"
    exe.write_bytes(b"MZ" + b"\0" * (size - 2))
    return exe


def test_a_whole_installer_is_accepted_and_gets_a_checksum(tmp_path: Path) -> None:
    exe = _fake_installer(tmp_path, 25_000_000)
    result = _run_bash(_publish_step_script(), tmp_path, {})
    assert result.returncode == 0, result.stderr
    sums = (tmp_path / "dist" / "SHA256SUMS").read_text()
    # Bare name: whoever downloads the installer and this file into one folder must be
    # able to verify them without knowing what the CI workspace was called. GNU
    # sha256sum marks binary mode with a leading '*' on Windows and not on Linux.
    recorded = sums.split()[1].lstrip("*")
    assert recorded == "SyntH-Setup-1.0.16.exe"
    # The checksum must be true, not merely present.
    check = subprocess.run(
        ["bash", "-c", "sha256sum -c SHA256SUMS"],
        cwd=tmp_path / "dist",
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert exe.stat().st_size == 25_000_000


def test_a_truncated_installer_is_refused(tmp_path: Path) -> None:
    """A published asset is what strangers download and cannot be quietly corrected."""
    _fake_installer(tmp_path, 1_000_000)
    result = _run_bash(_publish_step_script(), tmp_path, {})
    assert result.returncode != 0
    assert "not a whole installer" in result.stdout + result.stderr
    # Nothing may be published from a run that refused: no checksum file either.
    assert not (tmp_path / "dist" / "SHA256SUMS").exists()


def test_nothing_to_publish_is_an_error_not_a_silent_success(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    result = _run_bash(_publish_step_script(), tmp_path, {})
    assert result.returncode != 0
    assert "nothing to publish" in result.stdout + result.stderr


# --------------------------------------------------------------------------- structure


def test_the_release_job_consumes_the_artifact_the_build_produces() -> None:
    """A name mismatch here publishes nothing, and the run would still look green."""
    uploaded = _step("windows-installer", "Upload the installer")["with"]["name"]
    fetched = _step("release", "Fetch the installer this run built")["with"]["name"]
    assert uploaded == fetched
    assert "needs.prepare.outputs.version" in uploaded


def test_only_main_and_develop_release_and_a_pull_request_does_not() -> None:
    """main makes valid releases, develop pre-releases, nothing else a release."""
    condition = str(_job("release").get("if", ""))
    assert "pull_request" in condition and "!=" in condition
    assert "github.ref_name == 'main'" in condition
    assert "github.ref_name == 'develop'" in condition
    assert "feat" not in condition and "refs/tags" not in condition
    # Writing a release needs contents: write; the build jobs only read.
    assert _job("release")["permissions"]["contents"] == "write"
    assert _job("prepare")["permissions"]["contents"] == "read"


def test_main_is_a_valid_release_and_develop_is_not() -> None:
    publish = _step("release", "Publish")["with"]
    # Only main is a release; the pre-release flag is on for every other branch that gets here.
    assert publish["prerelease"] == "${{ github.ref_name != 'main' }}"
    assert publish["make_latest"] == "${{ github.ref_name == 'main' }}"
    assert publish["tag_name"] == "${{ env.RELEASE_TAG }}"
    assert publish["body_path"] == "RELEASE.md"
    # The notes are ours, from the commits: GitHub's own generator must stay off.
    assert not publish.get("generate_release_notes")
    notes = _step("release", "Release notes")["run"]
    assert "scripts/release_notes.py" in notes and ".github/release-header.md" in notes


def test_the_release_tag_is_the_version() -> None:
    expression = _job("release")["env"]["RELEASE_TAG"]
    assert "format('v{0}', needs.prepare.outputs.version)" in expression
    # It must not look like a plain version, or version.py would count it as a release.
    assert not re.fullmatch(r"v\d+\.\d+\.\d+", "v1.31.0-develop")


def test_develop_also_has_one_rolling_pre_release_and_keeps_the_versioned_one() -> None:
    """latest-develop is deleted and made again; the versioned release is never deleted."""
    remove = _step("release", "Remove the previous rolling pre-release")
    assert remove["if"] == "github.ref_name == 'develop'"
    assert (
        "gh release delete latest-develop" in remove["run"]
        and "--cleanup-tag" in remove["run"]
    )
    rolling = _step("release", "Publish the rolling pre-release")
    assert rolling["if"] == "github.ref_name == 'develop'"
    assert rolling["with"]["tag_name"] == "latest-develop"
    assert (
        rolling["with"]["prerelease"] is True
        and rolling["with"]["make_latest"] is False
    )
    steps = [x.get("name") for x in _job("release")["steps"]]
    assert (
        steps.index("Publish")
        < steps.index(remove["name"])
        < steps.index(rolling["name"])
    )
    # Only the rolling tag may ever be deleted.
    for step in _job("release")["steps"]:
        run = str(step.get("run", ""))
        if "gh release delete" in run:
            assert "latest-develop" in run, step.get("name")
    assert not re.fullmatch(r"v\d+\.\d+\.\d+", "latest-develop")


def test_pushes_to_the_rolling_release_do_not_overlap() -> None:
    group = _job("release")["concurrency"]
    assert group["cancel-in-progress"] is False
    assert "github.ref_name" in group["group"]


def test_the_notes_see_the_whole_history_and_its_tags() -> None:
    """A shallow checkout has no previous release tag and would list everything."""
    for job in ("prepare", "release"):
        checkout = _job(job)["steps"][0]
        assert checkout["with"]["fetch-depth"] == 0, job


def test_the_release_waits_for_the_images_without_needing_them() -> None:
    """`manifest` is skipped on a fork; a skipped need must not skip the release."""
    release = _job("release")
    assert "manifest" in release["needs"]
    assert "skipped" in str(release["if"]) and "!cancelled()" in str(release["if"])


def test_the_release_carries_the_installer_and_its_checksum() -> None:
    files = _step("release", "Publish")["with"]["files"]
    assert "*.exe" in files and "SHA256SUMS" in files
    assert _step("release", "Publish")["with"]["fail_on_unmatched_files"] is True


def test_the_workflow_runs_on_pushes_not_on_tags() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as the boolean True, which is a YAML 1.1 wart.
    triggers = workflow.get("on", workflow.get(True))
    # The release creates the tag itself; a pushed tag would start a second build of it.
    assert "tags" not in triggers["push"]
    assert "develop" in triggers["push"]["branches"]
    assert "main" in triggers["push"]["branches"]


def test_the_installer_version_is_the_one_that_was_decided() -> None:
    """Both consumers of the version must read the decided one, not two sources."""
    build = _step("windows-installer", "Build the installer")["env"]["VERSION"]
    uploaded = _step("windows-installer", "Upload the installer")["with"]["name"]
    assert build == "${{ needs.prepare.outputs.version }}"
    assert "${{ needs.prepare.outputs.version }}" in uploaded
    if os.name != "nt":
        assert not re.search(r"out\.txt", str(WORKFLOW.read_text(encoding="utf-8")))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))


def test_no_fork_run_is_failed_by_a_missing_docker_hub_secret() -> None:
    """A fork does not inherit this repository's secrets.

    A job that pushes to or pulls from Docker Hub with those credentials fails there for a
    reason that has nothing to do with the code, and if it carries no `continue-on-error`
    it turns the whole run red: `manifest` did exactly that, and it was the only reason a
    fork's run could never be green. Every job that touches those credentials must
    therefore either ignore its own failure or be gated on the secret being present.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    gate = "have-dockerhub"
    unguarded: list[str] = []

    for name, job in workflow["jobs"].items():
        if name == gate:
            continue  # this job exists to answer that question without ever failing
        if "DOCKERHUB" not in yaml.safe_dump(job):
            continue
        if job.get("continue-on-error") is True:
            continue
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        if gate in needs and gate in str(job.get("if", "")):
            continue
        unguarded.append(name)

    assert not unguarded, (
        f"these jobs need Docker Hub credentials and may not fail: {unguarded}. A fork has "
        "no such secret, so the run goes red there for a reason that is not code."
    )
