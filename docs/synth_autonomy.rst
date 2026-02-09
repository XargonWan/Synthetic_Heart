Synth Autonomy Mode
====================

**Description**: Controls how proactive the synth can be when interacting with users.

Default: ``suggest``

Allowed values:

- ``passive`` — The synth only responds when explicitly addressed.
- ``suggest`` — The synth may propose actions but will not execute them automatically by default.
- ``whitelisted`` — The synth may execute only actions listed in ``AUTONOMY_ALLOWED_ACTIONS`` (when the list is non-empty).
- ``autonomous`` — The synth may execute actions autonomously without restrictions (full autonomy). Ensure you understand the risks before enabling.

Notes
-----

- The default value is set to ``suggest`` to encourage proposal-first behavior and reduce risk from LLM-driven automatic actions.
- Operators may change this via the Web UI or API under the Synth → Persona settings.
