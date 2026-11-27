"""The extension Store in the WebUI (Plugins > Store).

The same installer as the command line, behind the WebUI token. Every route does its
blocking work (disk, network) off the event loop and answers ``{"ok": bool, ...}`` with
one-line errors; nothing from a repository is ever put in the page as HTML (the Core's
script builds the page with ``textContent``).

After a change the running extension is stopped, replaced and started again, so the
Store takes effect without restarting SyntH.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.extensions.repo import index as repo_index
from core.extensions.repo.config import ConfigError, Repository
from core.extensions.repo.installer import InstallError, Installer, UntrustedRepository
from core.extensions.repo.transports import guess_kind
from core.extensions.runtime import ExtensionRuntime

ROUTE = "/api/extensions/store"
SCRIPT_NAME = "extension_store"
SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "res"
    / "synth_webui"
    / "js"
    / "extension_store.js"
)


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _public_repo(repo: Repository) -> dict[str, Any]:
    data = asdict(repo)
    data.pop("token", None)
    data["has_token"] = bool(repo.token)
    data["fingerprint"] = (
        repo_index.key_fingerprint(repo.public_key)[:16] if repo.public_key else ""
    )
    data.pop("public_key", None)
    return data


def build_router(
    installer: Installer, runtime: Callable[[], ExtensionRuntime | None]
) -> APIRouter:
    from core import webui_auth

    router = APIRouter()

    def denied() -> JSONResponse:
        return JSONResponse({"detail": "Invalid or missing API token"}, status_code=401)

    async def body_of(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a bad body is empty
            return {}
        return body if isinstance(body, dict) else {}

    async def reload(ext_id: str, *, gone: bool = False) -> str:
        live = runtime()
        if live is None:
            return "the extension runtime is not running: it will start with SyntH"
        if gone:
            await live.deactivate(ext_id)
            return "stopped"
        return await live.activate(ext_id)

    @router.get(ROUTE)
    async def overview(request: Request, refresh: bool = False) -> Any:
        if not webui_auth.request_authorized(request):
            return denied()
        # The repositories are a local list: they are always there to be shown, added to
        # and removed, whatever a repository's index (a network read) does.
        try:
            rows, problems = await asyncio.to_thread(installer.rows, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 - an unreadable catalog is a message
            rows, problems = (
                [],
                [f"the catalog could not be read: {type(exc).__name__}: {exc}"],
            )
        try:
            state_count = len(installer.state.load().extensions)
        except Exception:  # noqa: BLE001
            state_count = 0
        try:
            repositories = [_public_repo(r) for r in installer.repos.load()]
        except Exception as exc:  # noqa: BLE001 - a damaged list is a message, not a 500
            repositories = []
            problems = [
                *problems,
                f"the list of repositories could not be read: {type(exc).__name__}: {exc}",
            ]
        return {
            "extensions": [asdict(r) for r in rows],
            "repositories": repositories,
            "problems": problems,
            "installed_count": state_count,
        }

    async def act(request: Request, work: Callable[[dict[str, Any]], Any]) -> Any:
        if not webui_auth.request_authorized(request):
            return denied()
        body = await body_of(request)
        try:
            return {"ok": True, **(await asyncio.to_thread(work, body))}
        except UntrustedRepository as exc:
            return {"ok": False, "needs_confirmation": True, "error": str(exc)}
        except (InstallError, ConfigError) as exc:
            return {"ok": False, "error": str(exc)}

    @router.post(f"{ROUTE}/install")
    async def install(request: Request) -> Any:
        holder: dict[str, str] = {}

        def work(body: dict[str, Any]) -> dict[str, Any]:
            ext_id = _text(body, "id")
            holder["id"] = ext_id
            result = installer.install(
                ext_id,
                version=_text(body, "version") or None,
                repo=_text(body, "repo") or None,
                allow_untrusted=body.get("confirm_untrusted") is True,
            )
            granted: list[str] = []
            if body.get("grant") is True:
                for line in result.installed:
                    granted += installer.grant(line.split()[0])
            holder["ids"] = " ".join(line.split()[0] for line in result.installed)
            return {
                "installed": result.installed,
                "warnings": result.warnings,
                "requested": list(installer.requested_capabilities(ext_id)),
                "granted": granted,
            }

        reply = await act(request, work)
        if isinstance(reply, dict) and reply.get("ok"):
            reply["runtime"] = {
                i: await reload(i) for i in holder.get("ids", "").split()
            }
        return reply

    @router.post(f"{ROUTE}/update")
    async def update(request: Request) -> Any:
        holder: dict[str, str] = {}

        def work(body: dict[str, Any]) -> dict[str, Any]:
            ext_id = _text(body, "id")
            holder["id"] = ext_id
            result = installer.update(
                ext_id, allow_untrusted=body.get("confirm_untrusted") is True
            )
            return {"installed": result.installed, "warnings": result.warnings}

        reply = await act(request, work)
        if isinstance(reply, dict) and reply.get("ok") and reply.get("installed"):
            reply["runtime"] = await reload(holder["id"])
        return reply

    @router.get(f"{ROUTE}/icon/{{publisher}}/{{name}}")
    async def icon(publisher: str, name: str) -> Any:
        """The icon of an installed extension (its ``icon.svg`` or ``icon.png``), behind the
        WebUI's own gate like every plugin icon; one that is not installed has none yet."""
        from fastapi.responses import FileResponse

        ext_id = f"{publisher}/{name}"
        try:
            base = installer.install_dir(ext_id)
        except Exception:  # noqa: BLE001 - not a valid id
            return JSONResponse({"detail": "Not found"}, status_code=404)
        for filename, media in (
            ("icon.svg", "image/svg+xml"),
            ("icon.png", "image/png"),
        ):
            path = base / filename
            if path.is_file() and not path.is_symlink():
                return FileResponse(path, media_type=media)
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @router.get(f"{ROUTE}/updates")
    async def updates(request: Request, refresh: bool = False) -> Any:
        """What has a newer version in a repository: ``[{id, installed, available}]``."""
        if not webui_auth.request_authorized(request):
            return denied()
        try:
            found = await asyncio.to_thread(installer.updates, refresh=refresh)
            problem = ""
        except Exception as exc:  # noqa: BLE001 - an unreachable repository is a message
            found, problem = [], f"{type(exc).__name__}: {exc}"
        return {
            "updates": [
                {"id": i, "installed": have, "available": new} for i, have, new in found
            ],
            "problem": problem,
        }

    @router.post(f"{ROUTE}/update-all")
    async def update_all(request: Request) -> Any:
        """Update every extension that has a newer version. One that fails (or comes from a
        repository that needs a confirmation) does not stop the others."""
        if not webui_auth.request_authorized(request):
            return denied()

        def work() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for ext_id, have, new in installer.updates():
                try:
                    done = installer.update(ext_id)
                    results.append(
                        {
                            "id": ext_id,
                            "ok": True,
                            "from": have,
                            "to": new,
                            "installed": done.installed,
                        }
                    )
                except UntrustedRepository as exc:
                    results.append(
                        {
                            "id": ext_id,
                            "ok": False,
                            "needs_confirmation": True,
                            "error": str(exc),
                        }
                    )
                except (InstallError, ConfigError) as exc:
                    results.append({"id": ext_id, "ok": False, "error": str(exc)})
            return results

        try:
            results = await asyncio.to_thread(work)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "results": []}
        for entry in results:
            if entry["ok"]:
                entry["runtime"] = await reload(entry["id"])
        return {
            "ok": all(r["ok"] for r in results),
            "updated": sum(1 for r in results if r["ok"]),
            "results": results,
        }

    @router.post(f"{ROUTE}/rollback")
    async def rollback(request: Request) -> Any:
        holder: dict[str, str] = {}

        def work(body: dict[str, Any]) -> dict[str, Any]:
            holder["id"] = _text(body, "id")
            return {"version": installer.rollback(holder["id"])}

        reply = await act(request, work)
        if isinstance(reply, dict) and reply.get("ok"):
            reply["runtime"] = await reload(holder["id"])
        return reply

    @router.post(f"{ROUTE}/remove")
    async def remove(request: Request) -> Any:
        holder: dict[str, str] = {}

        def work(body: dict[str, Any]) -> dict[str, Any]:
            holder["id"] = _text(body, "id")
            installer.remove(holder["id"], purge=body.get("purge") is True)
            return {}

        # Stop it first: its files are about to disappear under a running process.
        if webui_auth.request_authorized(request):
            peek = await body_of(request)
            ext_id = _text(peek, "id")
            if ext_id and ext_id in installer.state.load().extensions:
                await reload(ext_id, gone=True)
        reply = await act(request, work)
        if isinstance(reply, dict) and not reply.get("ok") and holder.get("id"):
            await reload(holder["id"])  # the removal was refused: bring it back
        return reply

    for name, enabled in (("enable", True), ("disable", False)):

        def make(on: bool, label: str) -> None:
            @router.post(f"{ROUTE}/{label}")
            async def toggle(request: Request) -> Any:
                holder: dict[str, str] = {}

                def work(body: dict[str, Any]) -> dict[str, Any]:
                    holder["id"] = _text(body, "id")
                    installer.set_enabled(holder["id"], on)
                    return {}

                reply = await act(request, work)
                if isinstance(reply, dict) and reply.get("ok"):
                    if on:
                        reply["runtime"] = await reload(holder["id"])
                    else:
                        await reload(holder["id"], gone=True)
                return reply

        make(enabled, name)

    @router.post(f"{ROUTE}/grant")
    async def grant(request: Request) -> Any:
        def work(body: dict[str, Any]) -> dict[str, Any]:
            caps = body.get("capabilities")
            wanted = (
                caps
                if isinstance(caps, list) and all(isinstance(c, str) for c in caps)
                else None
            )
            return {"granted": installer.grant(_text(body, "id"), wanted)}

        return await act(request, work)

    @router.post(f"{ROUTE}/repo/add")
    async def repo_add(request: Request) -> Any:
        def work(body: dict[str, Any]) -> dict[str, Any]:
            location = _text(body, "location")
            kind = _text(body, "kind") or guess_kind(location)
            key = _text(body, "key")
            repo = Repository(
                name=_text(body, "name"),
                location=location
                if kind != "local"
                else str(Path(location).expanduser().resolve()),
                kind=kind,
                priority=int(body["priority"])
                if isinstance(body.get("priority"), int)
                else 100,
                public_key=key,
                require_signature=bool(key) and body.get("allow_unsigned") is not True,
                trusted=body.get("trusted") is True,
                branch=_text(body, "branch"),
            )
            installer.repos.add(repo)
            try:
                index, signed = installer.load_index(repo, refresh=True)
            except InstallError:
                installer.repos.remove(repo.name)
                raise
            return {"packages": len(index.packages), "signed": signed}

        return await act(request, work)

    @router.post(f"{ROUTE}/repo/remove")
    async def repo_remove(request: Request) -> Any:
        def work(body: dict[str, Any]) -> dict[str, Any]:
            if not installer.repos.remove(_text(body, "name")):
                raise ConfigError("no such repository")
            return {}

        return await act(request, work)

    return router


def mount(installer: Installer, runtime: Callable[[], ExtensionRuntime | None]) -> bool:
    """Mount the Store routes and script on the running WebUI. ``False`` if it is not up."""
    try:
        from core.webui import synth_webui_interface
    except Exception:  # noqa: BLE001
        return False
    if synth_webui_interface is None:
        return False
    synth_webui_interface.register_plugin_router(
        build_router(installer, runtime), "extension store"
    )
    synth_webui_interface.register_plugin_js(
        SCRIPT_NAME, SCRIPT_PATH.read_text(encoding="utf-8")
    )
    return True


async def mount_when_ready(
    installer: Installer,
    runtime: Callable[[], ExtensionRuntime | None],
    tries: int = 60,
) -> bool:
    for _ in range(tries):
        if mount(installer, runtime):
            return True
        await asyncio.sleep(2)
    return False
