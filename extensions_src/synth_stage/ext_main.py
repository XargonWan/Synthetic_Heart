"""SyntH Stage extension.

The client is a static single-page app: the Core serves the ``web/`` directory (see
``[provides.web]`` in the manifest), so this process has nothing to do but stay up.
That is what "on" means for an extension, and the Core only serves files of one that
is running and has been granted ``web.static``.
"""

from __future__ import annotations

from synth_sdk.runtime import Context


async def main(ctx: Context) -> None:
    ctx.log.info("serving the Stage client at /stage/")
    await ctx.stopping.wait()
