Window Manager (WinBox)
=========================

Overview
--------

The WebUI uses **WinBox.js** as the window manager for floating panels (chat,
web debug, archives) while preserving the existing color palette. The default
WinBox minimize behavior is disabled and replaced with **custom circular
icons** to match the current UI.

Key behaviors
-------------

- **Minimize**: windows are hidden and represented by circular icons in the
  minimized stack.
- **Restore**: clicking a circular icon restores the window and focuses it.
- **Maximize**: handled via the window manager API (still available even when
  the WinBox header is hidden).
- **Drag**: the chat title bar remains the primary drag handle for moving the
  window, even when WinBox controls are hidden.
- **Mobile**: WinBox is disabled on narrow/touch viewports and the WebUI
  falls back to the previous fixed layout.

Design notes
------------

- The frame border is **visible but subtle**, using a low-contrast border to
  avoid strong visual separation from the existing UI.
- Window background and text colors follow the current WebUI theme variables.

Implementation summary
----------------------

- WinBox assets are loaded in the WebUI shells.
- A lightweight window manager wrapper exposes:

  - ``window.SynthWindowManager.create(...)``
  - ``window.SynthWindowManager.minimize(id)``
  - ``window.SynthWindowManager.restore(id)``
  - ``window.SynthWindowManager.toggleMaximize(id)``

- The chat window is mounted into a WinBox instance with the header hidden,
  retaining the existing internal title bar and controls.

Customization
-------------

To adjust styles, edit the WinBox override rules and the minimized stack
container in the WebUI shell templates. The most relevant selectors are:

- ``.winbox.synth-winbox``
- ``.winbox.synth-winbox .wb-body``
- ``.synth-minimized-stack``
- ``#chat.synth-window-managed``

For additional windows (debug/archives), use the window manager API and
provide a custom icon label for the minimized stack.
