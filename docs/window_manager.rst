Window Manager (WinBox)
=========================

Overview
--------

The WebUI uses **WinBox.js** as the window manager for floating panels (chat,
web debug, archives) while preserving the existing color palette. Chat and
Debug now rely on the native WinBox titlebar controls; per-window custom
titlebar buttons are no longer injected on top of the standard controls.

Key behaviors
-------------

- **Minimize / restore**: available through the native WinBox controls and the
  shared ``SynthWindowManager`` wrapper.
- **Maximize**: handled via the window manager API and the native WinBox
  maximize control.
- **Restore after maximize**: the manager keeps a per-window normal rect and
  reapplies it when leaving the maximized state, including after a page reload.
- **Persistence across restarts**: window state and geometry are stored on
  stable per-window ``localStorage`` keys so the last non-maximized size can
  survive new chat sessions and container/browser restarts.
- **Reset position**: exposed from the Settings UI instead of a dedicated chat
  titlebar button. This is the only supported way to clear saved window
  geometry and force the defaults again.
- **Drag**: windows can be dragged normally within the desktop viewport, with
  top-bar and viewport clamping handled by the shared wrapper.
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

- Per-window geometry is persisted independently in ``localStorage``. When a
  window is maximized, the persisted rect remains the last non-maximized size
  and position so restore can return to the original layout instead of the
  maximized bounds. The primary storage keys are window-scoped rather than
  tied to a transient runtime session id, so layouts remain available after a
  container reboot until the user explicitly resets them from Settings.

Customization
-------------

To adjust styles, edit the WinBox override rules and the window-related layout
helpers in the WebUI shell templates. The most relevant selectors are:

- ``.winbox.synth-winbox``
- ``.winbox.synth-winbox .wb-body``
- ``.synth-minimized-stack``
- ``#chat.synth-window-managed``

For additional windows (debug/archives), use the window manager API and let
WinBox provide the standard titlebar controls unless a window has a very
specific accessibility or workflow need.

Runtime configuration & persistence
----------------------------------

- ``window.SynthConfig.WINDOW_DRAG_BOTTOM_OVERHANG`` (number, px, default: 180)
  - When set, allows windows to be dragged slightly below the viewport to
    accommodate UX workflows that benefit from a small overhang. This replaces
    the previous negative ``bottom`` override which caused maximize/restore
    calculations to be incorrect.

- Per-window state is now persisted independently (e.g. chat, debug). The
  state and rect keys stored in ``localStorage`` include the window id so
  restore/restoreState works for multiple windows.
