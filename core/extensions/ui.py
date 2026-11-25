"""Windows extensions put in the fixed slots of the WebUI.

Three slots exist (``manifest.UI_SLOTS``): ``activity`` and ``logs`` add a sub-tab to
those sections, ``more`` adds a line to the "More..." menu, which opens the window.
A window is a page of the extension's static files shown in a frame; the Core draws the
tab, the menu and the frame, so no extension script runs in the WebUI page itself.

Only a **running** extension that **declares** the window and has been **granted**
``web.static`` is listed. The frame is sandboxed unless the extension also has
``web.origin`` (see :mod:`core.extensions.web`).
"""

from __future__ import annotations

from html import escape
from typing import Any

from core.extensions import web
from core.extensions.gateway import Gateway

#: The template that holds each section's sub-tabs.
SECTION_OF_SLOT = {"activity": "history", "logs": "logs"}
SANDBOX = "allow-scripts allow-forms allow-popups allow-downloads"


def entries(gateway: Gateway) -> list[dict[str, Any]]:
    """Every window currently available, in a stable order."""
    found: list[dict[str, Any]] = []
    for extension_id in sorted(gateway.sessions):
        session = gateway.sessions[extension_id]
        provides = session.manifest.provides
        if provides.web is None or not provides.web.static:
            continue
        if not session.allowed(web.CAP_STATIC):
            continue
        same_origin = provides.web.same_origin and session.allowed(web.CAP_ORIGIN)
        for window in provides.ui:
            found.append(
                {
                    "extension_id": extension_id,
                    "slot": window.slot,
                    "id": window.id,
                    "title": window.title,
                    "icon": window.icon,
                    "url": f"/x/{extension_id}/{window.page}",
                    "same_origin": same_origin,
                }
            )
    return found


def frame_html(entry: dict[str, Any]) -> str:
    """The frame that shows a window. Every value is escaped: a title or an id is the
    extension's text, never markup."""
    sandbox = SANDBOX + (" allow-same-origin" if entry["same_origin"] else "")
    return (
        f'<iframe class="ext-frame" title="{escape(entry["title"])}" '
        f'src="{escape(entry["url"])}" sandbox="{sandbox}" loading="lazy" '
        'referrerpolicy="no-referrer"></iframe>'
    )


def section_tabs(gateway: Gateway, section: str) -> list[dict[str, str]]:
    """The sub-tabs to add to a section template (``history`` or ``logs``), each as
    ``{"tab_id", "button_html", "panel_html"}``."""
    tabs: list[dict[str, str]] = []
    for entry in entries(gateway):
        if SECTION_OF_SLOT.get(entry["slot"]) != section:
            continue
        key = f"ext-{entry['extension_id'].replace('/', '-')}-{entry['id']}"
        label = escape(f"{entry['icon']} {entry['title']}".strip())
        if section == "logs":
            button = (
                f'<button class="logs-subnav-btn" type="button" data-logs-subtab="{key}" '
                f'aria-selected="false">{label}</button>'
            )
            panel = (
                f'<section class="logs-subpanel ext-slot" id="logs-panel-{key}" '
                f'data-logs-subtab="{key}">{frame_html(entry)}</section>'
            )
        else:
            button = (
                f'<button class="sub-nav-btn" data-subtab="{key}" role="tab" '
                f'aria-selected="false">{label}</button>'
            )
            panel = (
                f'<div class="sub-tab-panel ext-slot" id="subtab-{key}" '
                f'data-subtab="{key}">{frame_html(entry)}</div>'
            )
        tabs.append({"tab_id": key, "button_html": button, "panel_html": panel})
    return tabs
