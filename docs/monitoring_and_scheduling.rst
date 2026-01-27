Chat Update Checker
====================

The Chat Update Checker is a lightweight core service that periodically checks
whether any chats have had new non-self messages since the last check. It uses
the `chat_history_cache` table (message timestamps) as a source of truth and
filters out messages sent by the synth itself (sender_id/sender_name values of
``self`` or ``synth``). It provides an async API for other components (for
example, Grillo observer) to query the recent-activity state.

Configuration
-------------

- ``CHAT_UPDATE_CHECK_INTERVAL`` -- Interval in seconds between checks (default: ``60``).
- ``CHAT_UPDATE_CHECKER_ENABLED`` -- Enable or disable the periodic checker (default: ``True``).
- ``CHAT_UPDATE_CHECKER_AUTO_START`` -- If ``True``, the background checker will be started automatically at core initialization. **Default: ``False``**. When ``False``, components should call `check_for_updates_once()` on demand or implement their own per-plugin "since timestamp → now" checks.

API
---

- ``core.chat_update_checker.check_for_updates_once()`` -- Async helper that performs a single check and returns
  a dict with keys ``updated`` (bool), ``new_messages`` (list of `{chat_id, last_active}`), and ``last_checked`` (ISO timestamp).
- ``core.chat_update_checker.start_chat_update_checker()`` -- Start the periodic background task (used during core init).

Integration
-----------

The checker is started automatically by the core initializer. Components that need to query whether new messages exist
can call ``check_for_updates_once()`` directly.
