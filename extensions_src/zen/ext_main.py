"""Zen LLM Engine extension.

Zen is a companion container (``synth-zen-llm-engine``) that exposes an
OpenAI-compatible endpoint. All this extension does is make sure that endpoint is
registered with the Core and carries the settings Zen needs; the Core knows nothing
else about Zen.

Removed with this extension's arrival: the Selenium TTS plugins (``zen_ttsfree``,
``zen_elevenlabs``) and ``cleanup_chrome.sh``, in commit bb8db542 (find them with
``git log --diff-filter=D -- plugins_dev/zen_ttsfree.py``). They drove a browser from inside
SyntH and have no place in the Core; if they come back, they belong to the Zen ecosystem.

* On a fresh install (no endpoint at all) it creates ``zen-llm-engine``. It never adds
  one behind an operator who already configured others, and a Zen the operator deleted
  stays deleted while they have other endpoints.
* An existing endpoint only gets the settings it lacks; what the operator set is kept.
* An endpoint still pointing at the pre-rename container name is corrected: the old
  hostname no longer resolves on the Docker network.
"""

from __future__ import annotations

import convert

from synth_sdk.runtime import Context, PermissionDenied


async def main(ctx: Context) -> None:
    base_url = str(await ctx.config.get("ZEN_BASE_URL") or convert.DEFAULT_BASE_URL)
    current = await ctx.endpoints.get(convert.ENDPOINT_NAME)
    stale = bool(current.get("exists")) and convert.is_stale(
        str(current.get("base_url"))
    )
    try:
        done = await ctx.endpoints.ensure(
            convert.ENDPOINT_NAME,
            base_url,
            display_label=convert.LABEL,
            extra_config=convert.EXTRA_CONFIG,
            subsystem_map=convert.SUBSYSTEMS,
            only_if_registry_empty=True,
            update_base_url=stale,
        )
    except PermissionDenied as exc:
        # The operator keyed the endpoint themselves: it is theirs now.
        ctx.log.warning(f"leaving the endpoint alone: {exc}")
        done = {}
    if done.get("created"):
        ctx.log.info(f"registered the {convert.ENDPOINT_NAME} endpoint")
    elif done.get("base_url_changed"):
        ctx.log.info(f"corrected a stale base URL for {convert.ENDPOINT_NAME}")
    elif done.get("skipped"):
        ctx.log.info("other endpoints are configured: not adding Zen behind them")
    await ctx.stopping.wait()
