"""Tests for ``core.extensions.media``: files across the extension boundary (M3b)."""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from core.extensions import media as media_module
from core.extensions.core_interfaces import core_outbound_resolver
from core.extensions.media import (
    MediaBridge,
    MediaError,
    MediaLimits,
    open_inside,
    safe_name,
)

EXT = "dev/chat"
PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


class Rig:
    def __init__(self, root: Path, limits: MediaLimits | None = None) -> None:
        self.root = root
        self.sandbox = root / "sandbox"
        self.sandbox.mkdir()
        self.uid: int | None = None
        self.now = time.time()
        self.inbound_root = root / "media"
        self.bridge = MediaBridge(
            data_dir=lambda ext: root / "data" / ext,
            uid=lambda ext: self.uid,
            inbound_root=self.inbound_root,
            resolve_outbound=self.resolve,
            limits=limits,
            clock=lambda: self.now,
        )
        self.data = root / "data" / EXT
        self.data.mkdir(parents=True)

    def resolve(self, raw: str) -> tuple[Path | None, str | None]:
        path = Path(raw).resolve()
        if self.sandbox not in path.parents:
            return None, "Path is outside allowed roots"
        if not path.is_file():
            return None, "File does not exist"
        if path.name.startswith(".env"):
            return None, "Refusing to attach a credential or key file"
        return path, None

    def inbox(self, name: str, data: bytes = PNG) -> Path:
        path = self.data / "inbox" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        return path

    def sandbox_file(self, name: str, data: bytes = PNG) -> Path:
        path = self.sandbox / name
        path.write_bytes(data)
        return path


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def item(path: Path | str, **kw: Any) -> dict[str, Any]:
    return {"path": str(path), **kw}


class TestSafeName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("photo.png", "photo.png"),
            ("../../etc/passwd", "passwd"),
            ("..\\..\\win.ini", "win.ini"),
            ("my file (1).jpg", "my_file_1_.jpg"),
            ("", "attachment"),
            (None, "attachment"),
            ("///", "attachment"),
            (".hidden", "hidden"),
            ("émoji😀.png", "moji_.png"),
        ],
    )
    def test_names_are_never_paths(self, raw: object, expected: str) -> None:
        assert safe_name(raw) == expected

    def test_long_names_keep_a_short_extension(self) -> None:
        name = safe_name("a" * 300 + ".jpeg", limit=50)
        assert len(name) <= 50 and name.endswith(".jpeg")
        assert len(safe_name("b" * 300, limit=50)) == 50


class TestOpenInside:
    def test_a_regular_file_inside_is_opened(self, rig: Rig) -> None:
        path = rig.inbox("a.png")
        fd, size = open_inside(rig.data, str(path), owner_uid=None)
        os.close(fd)
        assert size == len(PNG)

    @pytest.mark.parametrize("raw", ["", "relative/file.png", "a\x00b", None, 5])
    def test_bad_strings(self, rig: Rig, raw: Any) -> None:
        with pytest.raises(MediaError):
            open_inside(rig.data, raw, owner_uid=None)

    def test_outside_the_data_directory(self, rig: Rig) -> None:
        outside = rig.root / "secret.txt"
        outside.write_text("top secret")
        for raw in (str(outside), "/etc/passwd", str(rig.data / ".." / "secret.txt")):
            with pytest.raises(MediaError, match="outside"):
                open_inside(rig.data, raw, owner_uid=None)

    def test_the_data_directory_itself_and_directories_are_refused(
        self, rig: Rig
    ) -> None:
        with pytest.raises(MediaError):
            open_inside(rig.data, str(rig.data), owner_uid=None)
        (rig.data / "sub").mkdir()
        with pytest.raises(MediaError, match="regular"):
            open_inside(rig.data, str(rig.data / "sub"), owner_uid=None)

    def test_a_symlink_to_a_file_outside_is_refused(self, rig: Rig) -> None:
        secret = rig.root / ".env"
        secret.write_text("DB_PASSWORD=hunter2")
        link = rig.data / "innocent.png"
        link.symlink_to(secret)
        with pytest.raises(MediaError, match="outside"):
            open_inside(rig.data, str(link), owner_uid=None)

    def test_a_symlinked_directory_that_leads_out_is_refused(self, rig: Rig) -> None:
        (rig.data / "d").symlink_to(rig.root)
        with pytest.raises(MediaError, match="outside"):
            open_inside(rig.data, str(rig.data / "d" / "secret.txt"), owner_uid=None)

    def test_a_symlink_to_a_file_that_is_also_inside_is_harmless(
        self, rig: Rig
    ) -> None:
        target = rig.inbox("real.png")
        link = rig.data / "alias.png"
        link.symlink_to(target)
        fd, _ = open_inside(rig.data, str(link), owner_uid=None)
        os.close(fd)

    def test_a_fifo_does_not_hang_and_is_refused(self, rig: Rig) -> None:
        fifo = rig.data / "pipe"
        os.mkfifo(fifo)
        started = time.monotonic()
        with pytest.raises(MediaError, match="regular"):
            open_inside(rig.data, str(fifo), owner_uid=None)
        assert time.monotonic() - started < 2

    def test_a_missing_file(self, rig: Rig) -> None:
        with pytest.raises(MediaError, match="does not exist"):
            open_inside(rig.data, str(rig.data / "nope.png"), owner_uid=None)

    def test_the_owner_is_checked_when_the_extension_has_its_own_user(
        self, rig: Rig
    ) -> None:
        path = rig.inbox("a.png")
        mine = os.stat(path).st_uid
        fd, _ = open_inside(rig.data, str(path), owner_uid=mine)
        os.close(fd)
        with pytest.raises(MediaError, match="not owned"):
            open_inside(rig.data, str(path), owner_uid=mine + 4242)

    def test_a_path_swapped_after_the_check_is_caught_by_the_final_check(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate the race: the file actually opened lives outside the directory."""
        path = rig.inbox("a.png")
        monkeypatch.setattr(media_module.os, "readlink", lambda p: "/etc/passwd")
        with pytest.raises(MediaError, match="outside"):
            open_inside(rig.data, str(path), owner_uid=None)

    def test_no_descriptor_leaks_on_refusal(self, rig: Rig) -> None:
        before = len(os.listdir("/proc/self/fd"))
        fifo = rig.data / "pipe"
        os.mkfifo(fifo)
        for _ in range(20):
            with pytest.raises(MediaError):
                open_inside(rig.data, str(fifo), owner_uid=None)
        assert len(os.listdir("/proc/self/fd")) <= before + 1


class TestImportInbound:
    @pytest.mark.asyncio
    async def test_the_core_gets_its_own_copy(self, rig: Rig) -> None:
        source = rig.inbox("photo.png")
        [result] = await rig.bridge.import_inbound(
            EXT, [item(source, filename="Holiday photo.png", mime_type="image/png")]
        )
        copy = Path(result["path"])
        assert copy != source and copy.read_bytes() == PNG and source.exists()
        assert rig.inbound_root in copy.parents
        assert copy.name.endswith("_Holiday_photo.png")
        assert result["mime_type"] == "image/png" and result["size"] == len(PNG)
        assert result["filename"] == "Holiday_photo.png"
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
        assert stat.S_IMODE(copy.parent.stat().st_mode) == 0o700

    @pytest.mark.asyncio
    async def test_the_copy_survives_the_extension_deleting_its_file(
        self, rig: Rig
    ) -> None:
        source = rig.inbox("a.png")
        [result] = await rig.bridge.import_inbound(EXT, [item(source)])
        source.unlink()
        assert Path(result["path"]).read_bytes() == PNG

    @pytest.mark.asyncio
    async def test_the_mime_type_is_validated_or_guessed(self, rig: Rig) -> None:
        a, b, c = (
            rig.inbox("a.png"),
            rig.inbox("b.mp3", b"ID3"),
            rig.inbox("c.bin", b"x"),
        )
        results = await rig.bridge.import_inbound(
            EXT,
            [
                item(a, mime_type="not a mime; drop table"),
                item(b),
                item(c, mime_type="application/x-custom+zip"),
            ],
        )
        assert [r["mime_type"] for r in results] == [
            "image/png",
            "audio/mpeg",
            "application/x-custom+zip",
        ]

    @pytest.mark.asyncio
    async def test_a_hostile_file_name_cannot_choose_where_the_copy_goes(
        self, rig: Rig
    ) -> None:
        source = rig.inbox("a.png")
        [result] = await rig.bridge.import_inbound(
            EXT, [item(source, filename="../../../../etc/cron.d/evil")]
        )
        copy = Path(result["path"])
        assert rig.inbound_root in copy.parents and copy.name.endswith("_evil")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make",
        [
            lambda r: "/etc/passwd",
            lambda r: str(r.root / "outside.png"),
            lambda r: "inbox/relative.png",
        ],
    )
    async def test_paths_the_extension_does_not_own_are_refused(
        self, rig: Rig, make: Any
    ) -> None:
        (rig.root / "outside.png").write_bytes(PNG)
        with pytest.raises(MediaError):
            await rig.bridge.import_inbound(EXT, [item(make(rig))])
        assert not rig.inbound_root.exists() or not list(rig.inbound_root.rglob("*.*"))

    @pytest.mark.asyncio
    async def test_it_is_all_or_nothing(self, rig: Rig) -> None:
        good = rig.inbox("good.png")
        with pytest.raises(MediaError):
            await rig.bridge.import_inbound(EXT, [item(good), item("/etc/passwd")])
        assert [p for p in rig.inbound_root.rglob("*") if p.is_file()] == []

    @pytest.mark.asyncio
    async def test_limits(self, tmp_path: Path) -> None:
        limits = MediaLimits(
            max_file_bytes=100,
            max_files=2,
            max_total_bytes=150,
            inbound_quota_bytes=260,
        )
        r = Rig(tmp_path, limits)
        big = r.inbox("big.bin", b"x" * 101)
        a, b, c = (
            r.inbox("a.bin", b"a" * 90),
            r.inbox("b.bin", b"b" * 90),
            r.inbox("c.bin", b"c" * 10),
        )
        with pytest.raises(MediaError, match="larger than"):
            await r.bridge.import_inbound(EXT, [item(big)])
        with pytest.raises(MediaError, match="at most 2"):
            await r.bridge.import_inbound(EXT, [item(a), item(b), item(c)])
        with pytest.raises(MediaError, match="too large in total"):
            await r.bridge.import_inbound(EXT, [item(a), item(b)])
        await r.bridge.import_inbound(EXT, [item(a)])  # 90 used
        await r.bridge.import_inbound(EXT, [item(b)])  # 180 used
        with pytest.raises(MediaError, match="quota"):
            await r.bridge.import_inbound(EXT, [item(a), item(c)])  # would be 280 > 260

    @pytest.mark.asyncio
    async def test_a_file_that_grows_while_it_is_copied_is_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The size read before the copy cannot be trusted: the file may be growing."""
        r = Rig(tmp_path, MediaLimits(max_file_bytes=100))
        growing = r.inbox("grow.bin", b"x" * 500)
        real_open = media_module.open_inside

        def lying(root: Path, raw: str, *, owner_uid: int | None) -> tuple[int, int]:
            fd, _size = real_open(root, raw, owner_uid=owner_uid)
            return fd, 10  # "it is only 10 bytes"

        monkeypatch.setattr(media_module, "open_inside", lying)
        with pytest.raises(MediaError, match="larger than"):
            await r.bridge.import_inbound(EXT, [item(growing)])
        assert [p for p in r.inbound_root.rglob("*") if p.is_file()] == []

    @pytest.mark.asyncio
    async def test_non_object_entries(self, rig: Rig) -> None:
        with pytest.raises(MediaError, match="object"):
            await rig.bridge.import_inbound(EXT, ["/tmp/x"])  # type: ignore[list-item]

    @pytest.mark.asyncio
    async def test_the_quota_is_per_extension(self, tmp_path: Path) -> None:
        r = Rig(tmp_path, MediaLimits(inbound_quota_bytes=100))
        other = tmp_path / "data" / "dev/other"
        other.mkdir(parents=True)
        (other / "x.bin").write_bytes(b"x" * 90)
        (r.data / "y.bin").write_bytes(b"y" * 90)
        await r.bridge.import_inbound(EXT, [item(r.data / "y.bin")])
        await r.bridge.import_inbound(
            "dev/other", [item(other / "x.bin")]
        )  # its own quota

    @pytest.mark.asyncio
    async def test_the_event_loop_is_not_blocked_by_a_big_copy(
        self, tmp_path: Path
    ) -> None:
        r = Rig(tmp_path, MediaLimits(max_file_bytes=64 * 1024 * 1024))
        big = r.inbox("big.bin", os.urandom(1024) * 20_000)  # ~20 MB
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.001)
                ticks += 1

        task = asyncio.create_task(ticker())
        await r.bridge.import_inbound(EXT, [item(big)])
        task.cancel()
        assert ticks > 3


class TestStageOutbound:
    @pytest.mark.asyncio
    async def test_files_are_copied_into_the_extensions_directory(
        self, rig: Rig
    ) -> None:
        a = rig.sandbox_file("report.pdf", b"%PDF-1.4 fake")
        b = rig.sandbox_file("pic.png")
        staged = await rig.bridge.stage_outbound(EXT, [str(a), str(b)])
        try:
            assert [i["name"] for i in staged.items] == ["report.pdf", "pic.png"]
            assert [i["kind"] for i in staged.items] == ["document", "image"]
            assert [i["mime_type"] for i in staged.items] == [
                "application/pdf",
                "image/png",
            ]
            for entry, original in zip(staged.items, (a, b), strict=True):
                copy = Path(entry["path"])
                assert rig.data in copy.parents and "outbox" in copy.parts
                assert copy.read_bytes() == original.read_bytes() and copy != original
                assert stat.S_IMODE(copy.stat().st_mode) == 0o600
                assert entry["size"] == original.stat().st_size
            assert staged.rejected == []
        finally:
            staged.cleanup()
        assert not any(rig.data.joinpath("outbox").iterdir())

    @pytest.mark.asyncio
    async def test_the_cores_sandbox_decides_what_may_leave(self, rig: Rig) -> None:
        ok = rig.sandbox_file("ok.png")
        secret = rig.sandbox_file(".env")
        outside = rig.root / "outside.txt"
        outside.write_text("x")
        staged = await rig.bridge.stage_outbound(
            EXT,
            [
                str(ok),
                str(secret),
                str(outside),
                str(rig.sandbox / "missing.png"),
                "/etc/passwd",
            ],
        )
        try:
            assert [i["name"] for i in staged.items] == ["ok.png"]
            reasons = dict(staged.rejected)
            assert "credential" in reasons[str(secret)]
            assert (
                "outside" in reasons[str(outside)]
                and "outside" in reasons["/etc/passwd"]
            )
            assert "does not exist" in reasons[str(rig.sandbox / "missing.png")]
        finally:
            staged.cleanup()

    @pytest.mark.asyncio
    async def test_nothing_is_created_when_everything_is_refused(
        self, rig: Rig
    ) -> None:
        staged = await rig.bridge.stage_outbound(EXT, ["/etc/passwd"])
        assert staged.items == [] and staged.directory is None
        assert not (rig.data / "outbox").exists()
        staged.cleanup()  # harmless

    @pytest.mark.asyncio
    async def test_limits(self, tmp_path: Path) -> None:
        r = Rig(
            tmp_path, MediaLimits(max_file_bytes=100, max_files=2, max_total_bytes=150)
        )
        big, a, b, c = (
            r.sandbox_file("big.bin", b"x" * 101),
            r.sandbox_file("a.bin", b"a" * 90),
            r.sandbox_file("b.bin", b"b" * 90),
            r.sandbox_file("c.bin", b"c" * 10),
        )
        staged = await r.bridge.stage_outbound(EXT, [str(big), str(a), str(b)])
        try:
            assert [i["name"] for i in staged.items] == ["a.bin"]
            reasons = dict(staged.rejected)
            assert reasons[str(big)] == "file too large"
            assert reasons[str(b)] == "too many attachments"
        finally:
            staged.cleanup()
        # The total size of one message is capped too.
        (tmp_path / "wide").mkdir()
        wide = Rig(
            tmp_path / "wide",
            MediaLimits(max_file_bytes=100, max_files=5, max_total_bytes=150),
        )
        wa = wide.sandbox_file("a.bin", b"a" * 90)
        wb = wide.sandbox_file("b.bin", b"b" * 90)
        total = await wide.bridge.stage_outbound(EXT, [str(wa), str(wb)])
        try:
            assert [i["name"] for i in total.items] == ["a.bin"]
            assert dict(total.rejected)[str(wb)] == "attachments too large in total"
        finally:
            total.cleanup()
        extra = await r.bridge.stage_outbound(EXT, [str(a), str(c), str(b)])
        try:
            assert [i["name"] for i in extra.items] == ["a.bin", "c.bin"]
            assert dict(extra.rejected)[str(b)] == "too many attachments"
        finally:
            extra.cleanup()

    @pytest.mark.asyncio
    async def test_copies_belong_to_the_extensions_user(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig.uid = 200_321
        chowned: list[tuple[str, int, int]] = []
        monkeypatch.setattr(
            media_module.os, "chown", lambda p, u, g: chowned.append((str(p), u, g))
        )
        monkeypatch.setattr(
            media_module, "prepare_data_dir", lambda d, uid: d.mkdir(exist_ok=True)
        )
        staged = await rig.bridge.stage_outbound(EXT, [str(rig.sandbox_file("a.png"))])
        try:
            names = [Path(p).name for p, _, _ in chowned]
            assert {u for _, u, _ in chowned} == {200_321} and {
                g for _, _, g in chowned
            } == {200_321}
            assert any(n == "outbox" for n in names) and any(
                n.endswith("_a.png") for n in names
            )
        finally:
            staged.cleanup()

    @pytest.mark.asyncio
    async def test_a_failure_while_copying_leaves_nothing_behind(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a: Any, **k: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(media_module.shutil, "copyfile", boom)
        with pytest.raises(OSError):
            await rig.bridge.stage_outbound(EXT, [str(rig.sandbox_file("a.png"))])
        assert not any((rig.data / "outbox").iterdir())

    @pytest.mark.asyncio
    async def test_with_the_real_core_sandbox_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = tmp_path / "agent_fs"
        sandbox.mkdir()
        monkeypatch.setenv("AGENT_FS_ROOTS", str(sandbox))
        good = sandbox / "chart.png"
        good.write_bytes(PNG)
        secret = sandbox / "service-account.json"
        secret.write_text("{}")
        bridge = MediaBridge(
            data_dir=lambda ext: tmp_path / "data" / ext,
            uid=lambda ext: None,
            inbound_root=tmp_path / "media",
            resolve_outbound=core_outbound_resolver,
        )
        staged = await bridge.stage_outbound(
            EXT, [str(good), str(secret), "/etc/hostname"]
        )
        try:
            assert [i["name"] for i in staged.items] == ["chart.png"]
            assert len(staged.rejected) == 2
        finally:
            staged.cleanup()


class TestPrune:
    @pytest.mark.asyncio
    async def test_expired_inbound_copies_and_stale_outbox_are_removed(
        self, rig: Rig
    ) -> None:
        [old] = await rig.bridge.import_inbound(EXT, [item(rig.inbox("old.png"))])
        [fresh] = await rig.bridge.import_inbound(EXT, [item(rig.inbox("new.png"))])
        long_ago = time.time() - 30 * 86400
        os.utime(old["path"], (long_ago, long_ago))
        stale = rig.data / "outbox" / "stale"
        stale.mkdir(parents=True)
        (stale / "f").write_text("x")
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        live = rig.data / "outbox" / "live"
        live.mkdir()
        removed = await rig.bridge.prune([EXT])
        assert removed == {"inbound": 1, "outbox": 1}
        assert not Path(old["path"]).exists() and Path(fresh["path"]).exists()
        assert not stale.exists() and live.exists()

    @pytest.mark.asyncio
    async def test_pruning_with_nothing_there_is_fine(self, tmp_path: Path) -> None:
        bridge = MediaBridge(
            data_dir=lambda ext: tmp_path / ext,
            uid=lambda ext: None,
            inbound_root=tmp_path / "does-not-exist",
            resolve_outbound=lambda p: (None, "no"),
        )
        assert await bridge.prune(["dev/none"]) == {"inbound": 0, "outbox": 0}
