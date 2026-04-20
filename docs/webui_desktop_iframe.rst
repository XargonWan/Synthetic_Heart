Desktop iframe (Top menu isolation)
=====================================

Overview
--------

Starting in commit `feat/agent` the WebUI supports an "iframe-based" desktop layout that
keeps the top navigation (header) and the rest of the page in separate browsing
contexts. This prevents content inside the desktop area from hiding, overlapping
or interfering with the top menu (header), improving reliability for features
that require the header to remain available (e.g. navigation, trainer controls,
approval buttons, etc.).

How it works
------------

- The server exposes a lightweight host page at ``/iframe/{section}`` which
  fetches and injects the modular ``/templates/{section}.html`` into the iframe's
  document.
- The main application inserts an iframe with ``id="desktop-iframe"`` into
  the page. When iframe mode is active the parent will hide the internal
  ``.tab-panel`` elements and allow the iframe to fill the desktop area.
- Navigation events (clicks on the top menu) are forwarded to the iframe via
  ``postMessage({ type: 'load', section: '<tab>' })`` so the iframe can load the
  requested section.

Configuration
-------------

- Server-side (render time): set ``__SYNTH_CONFIG.DESKTOP_IFRAME`` (boolean) to
  control the default behavior across deploys.
- Client-side override: set ``window.SynthConfig.DESKTOP_IFRAME = false``
  before the UI scripts load to disable iframe mode locally.
- Default: iframe mode is enabled by default.

Usage / Examples
----------------

- Open the standard UI and use the top menu. When iframe mode is enabled the
  desktop content is loaded inside the iframe rather than being injected into
  the parent document.

- From the browser console you can manually request the iframe to load a
  section (useful for testing):

.. code-block:: javascript

   // Request the iframe to load the 'history' section
   document.getElementById('desktop-iframe').contentWindow.postMessage({ type: 'load', section: 'history' }, window.location.origin);

- To disable iframe mode in your current session (e.g. for debugging):

.. code-block:: javascript

   window.SynthConfig.DESKTOP_IFRAME = false;
   location.reload();

Security & sandboxing
---------------------

- The iframe is created with a conservative sandbox attribute: ``sandbox="allow-scripts allow-same-origin"``.
- The iframe host and the parent page are served from the same origin by default
  which enables direct script execution and same-origin interactions; the
  code uses ``postMessage`` with ``window.location.origin`` as target origin for
  extra safety.
- If you change hosting or introduce cross-origin frames you must handle
  messaging and origin checks carefully to avoid introducing vulnerabilities.

Troubleshooting
---------------

- If the iframe doesn't load a section:
  - Open developer tools and check for failed requests to ``/iframe/<section>``
    or ``/templates/<section>.html``.
  - Confirm the requested ``section`` is one of the allowed, server-validated
    names (home, skins, logs, diary, history, config, components, settings, about, agent).
- If scripts inside the section don't run, check that the host page injects
  and executes inline and external scripts. The host is implemented to
  preserve script execution order to mimic normal template loading.

Future improvements
-------------------

- Add a Settings toggle to enable/disable iframe mode from the UI.
- Add tests for embedded flow and a small e2e test that asserts the header
  remains visible while different sections are loaded in the iframe.

Notes
-----

- This implementation is intentionally low-level and conservative: it prefers
  same-origin embedding and simple messaging to avoid extra complexity.
- If you prefer a non-iframe approach (CSS/z-index based isolation) it remains
  possible and could be offered as an alternative mode later on.