(function () {
  const BUTTON_ID = "history-radio-plugin-btn";
  const PANEL_ID = "subtab-radio";
  const STYLE_ID = "radio-host-activity-style";
  const OBSERVER_FLAG = "__radioHostActivityObserver";

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) {
      return;
    }

    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent =
      "#" + PANEL_ID + " .radio-status-bar {\n" +
      "  display: flex; flex-wrap: wrap; align-items: center; gap: 12px;\n" +
      "  padding: 12px 16px; background: var(--surface);\n" +
      "  border-radius: 10px; margin: 16px;\n" +
      "}\n" +
      "#" + PANEL_ID + " .radio-status-indicator {\n" +
      "  width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;\n" +
      "}\n" +
      "#" + PANEL_ID + " .radio-status-indicator.online { background: #22c55e; }\n" +
      "#" + PANEL_ID + " .radio-status-indicator.offline { background: #ef4444; }\n" +
      "#" + PANEL_ID + " .style-badge {\n" +
      "  display: inline-block; padding: 2px 8px;\n" +
      "  border-radius: 10px; font-size: 0.75em;\n" +
      "  background: var(--accent); color: var(--accent-contrast, #07070c);\n" +
      "}\n" +
      "#" + PANEL_ID + " .radio-track-line {\n" +
      "  font-weight: 600; margin: 6px 0 4px;\n" +
      "  color: var(--text);\n" +
      "}\n" +
      "#" + PANEL_ID + " .radio-banter-text {\n" +
      "  white-space: pre-wrap; word-break: break-word;\n" +
      "  color: var(--text-soft); font-size: 0.95em; line-height: 1.5;\n" +
      "}\n" +
      "#" + PANEL_ID + " .radio-audio-wrap {\n" +
      "  margin-top: 10px;\n" +
      "}\n" +
      "#" + PANEL_ID + " .radio-audio-player {\n" +
      "  width: 100%; max-width: 480px; height: 36px;\n" +
      "}\n" +
      "#" + PANEL_ID + " .empty-state {\n" +
      "  text-align: center; padding: 40px 20px; color: var(--text-soft);\n" +
      "}\n" +
      "#" + PANEL_ID + " .setup-card {\n" +
      "  max-width: 600px; margin: 40px auto;\n" +
      "  padding: 32px; background: var(--surface);\n" +
      "  border-radius: 12px; text-align: center;\n" +
      "}\n" +
      "#" + PANEL_ID + " .setup-card h3 {\n" +
      "  margin: 0 0 12px; font-size: 1.3em;\n" +
      "}\n" +
      "#" + PANEL_ID + " .setup-card p {\n" +
      "  margin: 0 0 8px; color: var(--text-soft); line-height: 1.5;\n" +
      "}\n" +
      "#" + PANEL_ID + " .setup-card .step {\n" +
      "  text-align: left; padding: 8px 0;\n" +
      "}\n" +
      "#" + PANEL_ID + " .setup-card .step-num {\n" +
      "  display: inline-block; width: 24px; height: 24px;\n" +
      "  line-height: 24px; text-align: center; border-radius: 50%;\n" +
      "  background: var(--accent); color: var(--accent-contrast, #07070c);\n" +
      "  font-size: 0.8em; font-weight: 700; margin-right: 8px;\n" +
      "}\n" +
      "#" + PANEL_ID + " .radio-summary {\n" +
      "  color: var(--text-soft); font-size: 0.9rem;\n" +
      "}\n";
    document.head.appendChild(style);
  }

  function hidePanel(panel) {
    panel.classList.remove("active");
    panel.style.display = "none";
    panel.style.visibility = "hidden";
    panel.style.opacity = "0";
  }

  function showPanel(panel) {
    panel.classList.add("active");
    panel.style.display = "flex";
    panel.style.visibility = "visible";
    panel.style.opacity = "1";
  }

  function getHistoryContext() {
    // Target the content section, not the sidebar nav button (both carry data-tab="history").
    const historyPanel = document.querySelector('section[data-tab="history"]');
    if (!historyPanel) {
      return null;
    }

    const subNav = historyPanel.querySelector(".sub-nav");
    const subTabsContainer = historyPanel.querySelector(".sub-tabs-container");
    if (!subNav || !subTabsContainer) {
      return null;
    }

    return { historyPanel, subNav, subTabsContainer };
  }

  function deactivateRadioTab(historyPanel) {
    const button = historyPanel.querySelector("#" + BUTTON_ID);
    const panel = historyPanel.querySelector("#" + PANEL_ID);

    if (button) {
      button.classList.remove("active");
      button.setAttribute("aria-selected", "false");
    }
    if (panel) {
      hidePanel(panel);
    }
  }

  function activateRadioTab(historyPanel) {
    // history.js handles panel show/hide and button active state via data-subtab convention.
    // We only need to trigger data loading here.
    var panel = historyPanel.querySelector("#" + PANEL_ID);
    if (!panel) {
      return;
    }
    loadRadioActivity(panel);
  }

  function ensureRadioActivityTab() {
    const context = getHistoryContext();
    if (!context) {
      return false;
    }

    injectStyles();

    let button = context.subNav.querySelector("#" + BUTTON_ID);
    if (!button) {
      button = document.createElement("button");
      button.id = BUTTON_ID;
      button.className = "sub-nav-btn";
      button.type = "button";
      button.setAttribute("aria-selected", "false");
      button.innerHTML = '<span class="icon">📻</span><span>Radio</span>';
      context.subNav.appendChild(button);
    }

    // Always bind the click handler (idempotent via dataset flag)
    if (!button.dataset.radioClickBound) {
      button.addEventListener("click", function (event) {
        event.preventDefault();
        activateRadioTab(context.historyPanel);
      });
      button.dataset.radioClickBound = "1";
    }

    let panel = context.subTabsContainer.querySelector("#" + PANEL_ID);
    if (!panel) {
      panel = document.createElement("div");
      panel.id = PANEL_ID;
      panel.className = "sub-tab-panel";
      panel.dataset.subtab = "radio";
      panel.innerHTML =
        '<div class="loading-state"><div class="loading-spinner"></div><p>Loading radio activity...</p></div>';
      context.subTabsContainer.appendChild(panel);
      hidePanel(panel);
    }

    if (!context.subNav.dataset.radioPluginBound) {
      context.subNav.addEventListener("click", function (event) {
        const builtInButton = event.target.closest(".sub-nav-btn[data-subtab]");
        // Only deactivate radio tab when a *different* sub-nav button is clicked.
        if (builtInButton && builtInButton.id !== BUTTON_ID) {
          deactivateRadioTab(context.historyPanel);
        }
      });
      context.subNav.dataset.radioPluginBound = "1";
    }

    return true;
  }

  function buildSetupCard() {
    return (
      '<div class="setup-card">' +
      '<h3>📻 Radio Host</h3>' +
      "<p>Synth can be your AI radio DJ, automatically generating spoken transitions between songs on your AzuraCast station.</p>" +
      "<hr style='margin: 20px 0; border-color: var(--border-color);'>" +
      "<div style='text-align: left;'>" +
      "<strong>To get started:</strong>" +
      "<div class='step'><span class='step-num'>1</span>Go to the <strong>Config</strong> tab</div>" +
      "<div class='step'><span class='step-num'>2</span>Fill in: <strong>AzuraCast Base URL</strong>, <strong>API Key</strong>, and <strong>Station ID</strong></div>" +
      "<div class='step'><span class='step-num'>3</span>Toggle <strong>Radio Host Enabled</strong> to ON</div>" +
      "</div>" +
      "</div>"
    );
  }

  function renderActivityEntry(activity) {
    var time = activity.timestamp
      ? new Date(activity.timestamp + (activity.timestamp.endsWith("Z") ? "" : "Z")).toLocaleString()
      : "—";
    var track = escapeHtml(
      (activity.track_artist || "?") + " — " + (activity.track_title || "?")
    );
    var banter = escapeHtml(activity.banter_text || "—");
    var styleBadge =
      '<span class="style-badge">' + escapeHtml(activity.style || "transition") + "</span>";

    var audioBlock = "";
    if (activity.audio_url) {
      audioBlock =
        '<div class="radio-audio-wrap">' +
        '<audio controls preload="none" class="radio-audio-player">' +
        '<source src="' + escapeHtml(activity.audio_url) + '" type="audio/wav">' +
        "</audio>" +
        "</div>";
    }

    return (
      '<div class="history-entry">' +
        '<div class="history-entry-header">' +
          '<div class="history-entry-meta">' +
            '<div class="history-entry-date">📻 ' + time + "</div>" +
            styleBadge +
          "</div>" +
        "</div>" +
        '<div class="radio-track-line">' + track + "</div>" +
        '<div class="radio-banter-text">' + banter + "</div>" +
        audioBlock +
      "</div>"
    );
  }

  function buildActivityView(data) {
    var statusClass = data.online ? "online" : "offline";
    var statusText = data.online ? "Connected" : "Disconnected";
    var intermissionInfo = data.intermission > 1
      ? "Every " + data.intermission + " songs"
      : "Every song";
    var stationName = data.station_name ? escapeHtml(data.station_name) : "Radio Host";
    var scheduleInfo = data.schedule_description
      ? " &middot; " + escapeHtml(data.schedule_description)
      : "";

    var statusBar =
      '<div class="radio-status-bar">' +
        '<span class="radio-status-indicator ' + statusClass + '"></span>' +
        "<strong>" + stationName + "</strong>" +
        " &middot; " + statusText +
        " &middot; " + escapeHtml(data.language || "English") +
        scheduleInfo +
        " &middot; Poll: " + data.poll_interval + "s" +
        " &middot; " + intermissionInfo +
      "</div>";

    var entriesHtml = "";
    if (data.activities && data.activities.length) {
      entriesHtml = data.activities.map(renderActivityEntry).join("");
    } else {
      entriesHtml =
        '<div class="empty-state">' +
        '<div class="icon">📻</div>' +
        "<p>No radio activity yet. Wait for a track change.</p>" +
        "</div>";
    }

    return (
      '<div class="section-controls">' +
        '<div class="radio-summary">Radio transitions and plugin status</div>' +
        '<button id="radio-reload-btn" class="action-btn" type="button" ' +
          'style="margin-left:auto;padding:4px 12px;font-size:0.85rem;">↺ Reload</button>' +
      "</div>" +
      statusBar +
      '<div id="radio-activity-list" class="history-container">' +
        entriesHtml +
      "</div>"
    );
  }

  function loadRadioActivity(panel) {
    panel.innerHTML =
      '<div class="loading-state"><div class="loading-spinner"></div><p>Loading radio activity...</p></div>';

    fetch("/api/radio/data")
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        if (!data.enabled || !data.configured) {
          panel.innerHTML = buildSetupCard();
          return;
        }
        panel.innerHTML = buildActivityView(data);
        // Wire reload button
        var reloadBtn = panel.querySelector("#radio-reload-btn");
        if (reloadBtn) {
          reloadBtn.addEventListener("click", function () {
            loadRadioActivity(panel);
          });
        }
      })
      .catch(function () {
        panel.innerHTML =
          '<div class="empty-state"><p>Could not load radio activity.</p></div>';
      });
  }

  function installObserver() {
    if (window[OBSERVER_FLAG]) {
      return;
    }

    const observer = new MutationObserver(function () {
      ensureRadioActivityTab();
    });
    observer.observe(document.body, { childList: true, subtree: true });
    window[OBSERVER_FLAG] = observer;
  }

  function escapeHtml(value) {
    if (!value) {
      return "";
    }
    return value
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // 1. Immediate attempt (handles case where section is already in the DOM)
  ensureRadioActivityTab();

  // 2. Wrap window.SynthWebUI.loadSection so we inject the Radio tab
  //    immediately after loadSection('history') resolves.  This is the most
  //    reliable hook because loadSection is set by main.js before radio_host.js
  //    runs, and the promise resolves only after the DOM is fully populated
  //    and initHistoryTab has already been called.
  (function () {
    var ns = window.SynthWebUI = window.SynthWebUI || {};
    if (ns._radioHookedLoadSection) { return; }
    ns._radioHookedLoadSection = true;
    var orig = ns.loadSection;
    ns.loadSection = function (section) {
      var p;
      try { p = orig ? orig.apply(this, arguments) : undefined; } catch (e) { }
      if (section === "history") {
        if (p && typeof p.then === "function") {
          p.then(function () { ensureRadioActivityTab(); });
        } else {
          ensureRadioActivityTab();
        }
      }
      return p;
    };
  })();

  // 3. Also intercept window.SynthWebUI.initHistoryTab as a secondary safety
  //    net (handles direct initHistoryTab() calls that bypass loadSection).
  //    The setter chains any future assignment with ensureRadioActivityTab.
  (function () {
    var ns = window.SynthWebUI = window.SynthWebUI || {};
    var _current = ns.initHistoryTab;

    function makeChained(originalFn) {
      return function () {
        try {
          if (typeof originalFn === "function") {
            originalFn.apply(this, arguments);
          }
        } catch (e) { /* ignore errors in original fn */ }
        ensureRadioActivityTab();
      };
    }

    _current = makeChained(_current);

    try {
      Object.defineProperty(ns, "initHistoryTab", {
        get: function () { return _current; },
        set: function (newVal) { _current = makeChained(newVal); },
        configurable: true,
        enumerable: true,
      });
    } catch (e) {
      ns.initHistoryTab = _current;
    }
  })();

  // 4. MutationObserver as a catch-all for any DOM insertions
  installObserver();

  // 5. DOMContentLoaded fallback
  document.addEventListener("DOMContentLoaded", function () {
    ensureRadioActivityTab();
  });
})();
