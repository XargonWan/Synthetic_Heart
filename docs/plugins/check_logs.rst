Check Logs Plugin
====================

The Check Logs plugin exposes two actions for LLM-initiated log inspection:

- ``get_logs``: return the last N lines from a log file (default 30)
- ``search_logs``: search log contents for keywords or regular expressions and return matching lines

Supported fields
-----------------

- ``file`` (optional): filename to read. Allowed values: ``synth.log``, ``prompt_cycle.log``, ``synth.log.1`` (and .2, .3), ``webui.log``, ``selkies.log``. Default: ``synth.log``.
- ``lines`` (optional): number of lines to return for ``get_logs`` (default: 30); for ``search_logs`` it's used to bound how many tail lines are searched (default: 30)
- ``queries`` (required for ``search_logs``): a string or list of strings to search for
- ``regex`` (optional for ``search_logs``): if true, queries are treated as regular expressions
- ``context`` (optional for ``search_logs``): number of surrounding lines to include for each match (default: 0)

Usage
-----

Call the action from the LLM with the appropriate payload. The plugin will send the results back to the invoking interface (e.g. Telegram) as a message.

Security
--------

Only pre-approved filenames under the configured ``SYNTH_LOG_DIR`` are allowed; path traversal is explicitly blocked.
