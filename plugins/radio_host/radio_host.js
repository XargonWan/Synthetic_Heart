(function () {
  if (document.querySelector('[data-tab="radio"]')) return;

  var nav = document.querySelector("nav.main-nav");
  if (!nav) return;
  var btn = document.createElement("button");
  btn.className = "nav-btn";
  btn.dataset.tab = "radio";
  btn.setAttribute("aria-controls", "tab-radio");
  btn.setAttribute("aria-pressed", "false");
  btn.innerHTML = '<span class="icon">📻</span><span>Radio</span>';
  nav.appendChild(btn);

  var main = document.querySelector("main");
  if (!main) return;
  var panel = document.createElement("section");
  panel.className = "tab-panel";
  panel.id = "tab-radio";
  panel.dataset.tab = "radio";
  panel.role = "tabpanel";
  main.appendChild(panel);

  var style = document.createElement("style");
  style.textContent =
    "#tab-radio .radio-status-bar {\n" +
    "  display: flex; align-items: center; gap: 12px;\n" +
    "  padding: 12px 16px; background: var(--surface-color);\n" +
    "  border-radius: 10px; margin-bottom: 16px;\n" +
    "}\n" +
    "#tab-radio .radio-status-indicator {\n" +
    "  width: 10px; height: 10px; border-radius: 50%;\n" +
    "  flex-shrink: 0;\n" +
    "}\n" +
    "#tab-radio .radio-status-indicator.online { background: #22c55e; }\n" +
    "#tab-radio .radio-status-indicator.offline { background: #ef4444; }\n" +
    "#tab-radio .radio-status-indicator.disabled { background: #6b7280; }\n" +
    "#tab-radio .radio-log-table {\n" +
    "  width: 100%; border-collapse: collapse;\n" +
    "}\n" +
    "#tab-radio .radio-log-table th,\n" +
    "#tab-radio .radio-log-table td {\n" +
    "  text-align: left; padding: 8px 12px;\n" +
    "  border-bottom: 1px solid var(--border-color);\n" +
    "}\n" +
    "#tab-radio .radio-log-table th {\n" +
    "  font-size: 0.8em; text-transform: uppercase;\n" +
    "  letter-spacing: 0.05em; color: var(--text-muted);\n" +
    "}\n" +
    "#tab-radio .banter-text {\n" +
    "  max-width: 400px; white-space: pre-wrap;\n" +
    "  word-break: break-word;\n" +
    "}\n" +
    "#tab-radio .style-badge {\n" +
    "  display: inline-block; padding: 2px 8px;\n" +
    "  border-radius: 10px; font-size: 0.75em;\n" +
    "  background: var(--accent-color); color: #fff;\n" +
    "}\n" +
    "#tab-radio .empty-state {\n" +
    "  text-align: center; padding: 40px 20px;\n" +
    "  color: var(--text-muted);\n" +
    "}\n" +
    "#tab-radio .setup-card {\n" +
    "  max-width: 600px; margin: 40px auto;\n" +
    "  padding: 32px; background: var(--surface-color);\n" +
    "  border-radius: 12px; text-align: center;\n" +
    "}\n" +
    "#tab-radio .setup-card h3 {\n" +
    "  margin: 0 0 12px; font-size: 1.3em;\n" +
    "}\n" +
    "#tab-radio .setup-card p {\n" +
    "  margin: 0 0 8px; color: var(--text-muted);\n" +
    "  line-height: 1.5;\n" +
    "}\n" +
    "#tab-radio .setup-card .step {\n" +
    "  text-align: left; padding: 8px 0;\n" +
    "}\n" +
    "#tab-radio .setup-card .step-num {\n" +
    "  display: inline-block; width: 24px; height: 24px;\n" +
    "  line-height: 24px; text-align: center;\n" +
    "  border-radius: 50%; background: var(--accent-color);\n" +
    "  color: #fff; font-size: 0.8em; font-weight: 700;\n" +
    "  margin-right: 8px;\n" +
    "}";

  document.head.appendChild(style);

  window.SynthWebUI = window.SynthWebUI || {};
  window.SynthWebUI.initRadioTab = function () {
    var panel = document.querySelector("#tab-radio");
    if (!panel) return;
    fetch("/api/radio/data")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var html = "";
        if (!data.enabled || !data.configured) {
          html = buildSetupCard(data);
        } else {
          html = buildActivityView(data);
        }
        panel.innerHTML = html;
        panel.dataset.loaded = "1";
      })
      .catch(function () {
        panel.innerHTML =
          '<div class="empty-state"><p>Could not load radio data.</p></div>';
      });
  };
  window.SynthWebUI.initRadioTab();

  function buildSetupCard(data) {
    return (
      '<div class="setup-card">' +
      '<h3>📻 Radio Host</h3>' +
      "<p>Synth can be your AI radio DJ, automatically generating " +
      "spoken transitions between songs on your AzuraCast station.</p>" +
      "<hr style='margin: 20px 0; border-color: var(--border-color);'>" +
      "<div style='text-align: left;'>" +
      "<strong>To get started:</strong>" +
      "<div class='step'><span class='step-num'>1</span>" +
      "Go to the <strong>Config</strong> tab</div>" +
      "<div class='step'><span class='step-num'>2</span>" +
      "Fill in: <strong>AzuraCast Base URL</strong>, " +
      "<strong>API Key</strong>, and <strong>Station ID</strong></div>" +
      "<div class='step'><span class='step-num'>3</span>" +
      "Toggle <strong>Radio Host Enabled</strong> to ON</div>" +
      "</div>" +
      "</div>"
    );
  }

  function buildActivityView(data) {
    var statusClass = data.online ? "online" : "offline";
    var statusText = data.online ? "Connected" : "Disconnected";
    var rows = "";
    if (data.activities && data.activities.length) {
      data.activities.forEach(function (a) {
        var time = a.timestamp
          ? new Date(a.timestamp + "Z").toLocaleString()
          : "—";
        var styleBadge =
          '<span class="style-badge">' + escapeHtml(a.style || "transition") +
          "</span>";
        var banter = escapeHtml(a.banter_text || "—");
        var track = escapeHtml(
          (a.track_artist || "?") + " — " + (a.track_title || "?")
        );
        rows +=
          "<tr><td>" + time + "</td><td>" + track + "</td>" +
          "<td class='banter-text'>" + banter + "</td>" +
          "<td>" + styleBadge + "</td></tr>";
      });
    } else {
      rows =
        '<tr><td colspan="4" class="empty-state">' +
        "No radio activity yet. Wait for a track change.</td></tr>";
    }
    var intermissionInfo = data.intermission > 1
      ? "Comment every " + data.intermission + " songs"
      : "Comment on every song";
    return (
      '<div class="radio-status-bar">' +
      '<span class="radio-status-indicator ' + statusClass + '"></span>' +
      "<strong>Radio Host</strong> &middot; " + statusText +
      " &middot; " + escapeHtml(data.language || "English") +
      " &middot; Poll: " + data.poll_interval + "s" +
      " &middot; " + intermissionInfo +
      "</div>" +
      '<div style="overflow-x: auto;">' +
      '<table class="radio-log-table">' +
      "<thead><tr><th>Time</th><th>Track</th><th>Banter</th><th>Style</th></tr></thead>" +
      "<tbody>" + rows + "</tbody>" +
      "</table></div>"
    );
  }

  function escapeHtml(str) {
    if (!str) return "";
    return str
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
})();
