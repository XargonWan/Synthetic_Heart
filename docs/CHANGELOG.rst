Changelog
=========

2025-12-15 - Bugfixes
---------------------

- Fix: WebUI chat resizing regression. Made the client-side `CHAT_RESIZABLE` variable mutable and exposed the `createChatResizeHandles()` function so runtime toggling and edge/corner resizing works as expected. (core/webui_templates/synth_webui_index.html)
- Fix: Discord interface routing bug where `interface_path` parsing could pass a list as channel id, causing send failures. Correctly unpack the parsed levels to extract the channel/thread id. (interface/discord_interface.py)
