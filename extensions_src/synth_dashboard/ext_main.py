"""Control Deck (Dashboard) extension.

The Dashboard is a static page: the Core serves the ``web/`` directory (see
``[provides.web]`` in the manifest) and the page talks to the WebUI's own ``/api`` as the
person using it. This process has nothing to do but stay up, which is what "on" means for
an extension.

The page began as the WebUI's built-in Dashboard tab; it was moved here with
``git log --follow -- extensions_src/synth_dashboard/web/dashboard.js``.
"""

from __future__ import annotations

from synth_sdk.runtime import Context


async def main(ctx: Context) -> None:
    ctx.log.info("serving the Control Deck")
    await ctx.stopping.wait()
