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

Endpoints: ``GET /api/synth_avatar`` (the PNG), ``GET /api/synth_avatar/info``
(version and which interfaces take it), ``POST``/``DELETE /api/synth_avatar``
(the latter two honour ``SYNTH_WEBUI_API_TOKEN``). The stored ``SYNTH_AVATAR``
value is the image version (a content hash).

Interface contract
------------------

Optional, structural (declared by method presence, see
``core/interface_capabilities.py``; the capability token is ``set_avatar``):

.. code-block:: python

   async def set_avatar(self, image_bytes: bytes | None, mime: str | None = None,
                        version: str | None = None) -> bool: ...
   def uses_synth_avatar(self) -> bool: ...   # optional opt-in switch

``image_bytes=None`` means the avatar was removed. ``set_avatar`` must not raise
for expected failures. Core calls every interface concurrently
(``core.synth_avatar.broadcast_avatar_changed``) with a per-interface timeout, so
one interface failing or hanging never affects the others or the save.
Interfaces should also apply the stored avatar on start-up and remember the
version they applied, to avoid re-uploading (providers rate-limit profile
changes).

Interface support
-----------------

.. list-table::
   :header-rows: 1

   * - Interface
     - Can set its avatar?
     - Status
   * - ``telegram`` (user account, Telethon)
     - Yes: ``photos.UploadProfilePhotoRequest`` / ``DeletePhotosRequest``
     - **Implemented**, opt-in ``TELEGRAM_USE_SYNTH_AVATAR`` (off by default: the
       picture is visible to all contacts). Applied at start-up and on change; the
       applied version is kept in ``TELEGRAM_AVATAR_APPLIED``.
   * - ``discord_bot`` (discord.py 2.7)
     - Yes: ``ClientUser.edit(avatar=bytes)``. Rate-limited (a few changes per
       hour), so apply from ``on_ready`` only when the version changed.
     - Not implemented yet.
   * - ``matrix_chat`` (matrix-nio 0.24)
     - Yes: ``AsyncClient.upload`` (unencrypted) then ``set_avatar(mxc://...)``.
     - Not implemented yet; hook after login in the sync loop.
   * - ``fluxer_bot``
     - Unknown: the client only calls ``/users/@me`` for reading. A Discord-like
       ``PATCH /users/@me`` is likely but unverified.
     - Not implemented; confirm the API first.
   * - ``telegram_bot`` (python-telegram-bot 22.6)
     - No: the Bot API wrapper in use has no own-profile-photo method (BotFather
       ``/setuserpic`` only).
     - Not supported.
   * - ``openai_api_server``, ``vessel_interface``, ``interface_dev/*``
     - No account/profile concept (or unverified).
     - Not applicable.
