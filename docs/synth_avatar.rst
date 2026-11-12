Synth Avatar
============

Core owns a single profile picture for the synth. Interfaces that can set an
account avatar use it as their own, and are updated whenever it changes.

Setting it
----------

WebUI → Settings → **Synth Avatar** (``SYNTH_AVATAR``, ``ui_type="avatar"``):

1. Choose an image (PNG, JPEG, WebP or GIF, up to 10 MB).
2. Drag it and zoom (wheel or slider) to frame it. The editor dims everything
   outside the circle, and two live previews show the **square** and the
   **round** result.
3. **Confirm and apply** uploads the framed 512x512 PNG. Core re-validates and
   re-encodes it (Pillow; centered square crop, EXIF rotation applied, metadata
   dropped), stores it as ``<data root>/avatar/synth_avatar.png`` and pushes it
   to the interfaces. The per-interface result is shown next to the buttons.
   **Remove avatar** clears it and notifies the interfaces too.
   **Resend avatar** pushes the stored picture to every opted-in interface again,
   ignoring the "already applied" shortcut: use it to bring back an account whose
   picture was changed elsewhere, to repair a push that failed, or to give a
   newly switched-on interface its first avatar. Under the buttons each interface
   shows **up to date**, **not applied yet** or **not enabled**.

All three buttons, and the browser's "Choose file" button, use the same
``.btn-ghost`` style as the rest of Settings. The editor is not special-cased in
``main.js``: it registers itself for ``ui_type="avatar"`` with
``window.registerUiType`` (``ui-helpers.js``), and both settings renderers look
the type up with ``window.getUiType``. Any exposed variable that needs its own
editor can do the same.

Endpoints: ``GET /api/synth_avatar`` (the PNG), ``GET /api/synth_avatar/info``
(version and, per interface, whether it opted in and which version it holds),
``POST``/``DELETE /api/synth_avatar`` and ``POST /api/synth_avatar/resend`` (these
honour ``SYNTH_WEBUI_API_TOKEN``). The stored ``SYNTH_AVATAR``
value is the image version (a content hash).

Interface contract
------------------

Optional, structural (declared by method presence, see
``core/interface_capabilities.py``; the capability token is ``set_avatar``):

.. code-block:: python

   async def set_avatar(self, image_bytes: bytes | None, mime: str | None = None,
                        version: str | None = None, force: bool = False) -> bool: ...
   def uses_synth_avatar(self) -> bool: ...   # optional opt-in switch

``force`` is optional (core passes it only to interfaces that accept it) and means
"the operator asked for a resend": skip any shortcut of your own.
``core.synth_avatar.register_interface_switch(interface_id)`` registers the
standard ``<INTERFACE>_USE_SYNTH_AVATAR`` option (shown in that interface's own
settings) and returns the reader to use in ``uses_synth_avatar``.

``image_bytes=None`` means the avatar was removed. ``set_avatar`` must not raise
for expected failures. Core calls every interface concurrently
(``core.synth_avatar.broadcast_avatar_changed``) with a per-interface timeout, so
one interface failing or hanging never affects the others or the save.
Core remembers, per interface, the version it last applied successfully
(``SYNTH_AVATAR_APPLIED__<interface>``) and does not push an unchanged picture
again (providers rate-limit profile changes; Discord allows only a few a hour). A
failed push records nothing, so it is retried on the next change.

Twenty seconds after start-up, and after an interface is switched on, core runs a
**catch-up**: it updates only interfaces that already received an avatar from here
and are now out of date. It never makes a *first* change to an account — that
takes an upload or **Resend avatar**.

Interface support
-----------------

Every interface below defaults to opted in (``<INTERFACE>_USE_SYNTH_AVATAR``)
except the Telegram user account, whose picture is visible to all contacts.

.. list-table::
   :header-rows: 1

   * - Interface
     - How
     - Status
   * - ``telegram`` (user account, Telethon)
     - ``photos.UploadProfilePhotoRequest`` / ``DeletePhotosRequest``
     - **Implemented**, opt-in ``TELEGRAM_USE_SYNTH_AVATAR`` (off by default).
       Applied at start-up and on change; also keeps ``TELEGRAM_AVATAR_APPLIED``.
   * - ``discord_bot`` (discord.py 2.7)
     - ``ClientUser.edit(avatar=bytes)``; a refused change (rate limit) is
       logged and retried on the next change or resend.
     - **Implemented**, ``DISCORD_BOT_USE_SYNTH_AVATAR``.
   * - ``matrix_chat`` (matrix-nio 0.24)
     - ``AsyncClient.upload`` then ``set_avatar(mxc://...)``. Removal is not
       applied (Matrix has no portable "no avatar").
     - **Implemented**, ``MATRIX_CHAT_USE_SYNTH_AVATAR``.
   * - ``fluxer_bot``
     - ``PATCH /users/@me`` with a data-URI avatar (Discord-compatible API).
     - **Implemented, unverified against a live Fluxer server**;
       ``FLUXER_BOT_USE_SYNTH_AVATAR``.
   * - ``telegram_bot`` (Bot API)
     - ``setMyProfilePhoto`` / ``removeMyProfilePhoto`` (Bot API 9.4), called
       directly because python-telegram-bot 22.6 does not wrap them. A Bot API
       server without them answers "not found" and the status is "not applied"
       (BotFather ``/setuserpic`` still works).
     - **Implemented**, ``TELEGRAM_BOT_USE_SYNTH_AVATAR``.
   * - ``synth_webui``
     - Reads ``GET /api/synth_avatar`` directly.
     - Not applicable.
   * - ``openai_api_server``, ``vessel_interface``, ``interface_dev/*``
     - No account/profile concept.
     - Not applicable.
