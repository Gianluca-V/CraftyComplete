"use strict";

// Status and power panel of the server page. Loaded after server.js (uses `base`).

const STATE_LABELS = {
  running: ["Running", "running-badge"],
  stopped: ["Stopped", "muted-badge"],
  starting: ["Starting", "warn-badge"],
  updating: ["Updating", "warn-badge"],
  crashed: ["Crashed", "crashed-badge"],
};
const POWER_CONFIRM = {
  stop: "Stop this server? Players online will be disconnected.",
  restart: "Restart this server? Players online will be disconnected.",
  kill: "Kill this server's process? Unsaved world changes can be lost.",
};
const STATUS_REFRESH_MS = 10000;

function statItem(label, value) {
  return el("div", {}, el("dt", {}, label), el("dd", {}, value == null || value === "" ? "—" : String(value)));
}

function renderStatus(status) {
  const [label, kind] = STATE_LABELS[status.state] || [status.state, "muted-badge"];
  const badgeNode = $("power-state");
  badgeNode.textContent = label;
  badgeNode.className = `badge ${kind}`;
  $("power-players").textContent = status.running
    ? `${status.players_online}/${status.max_players} players`
      + (status.players.length ? `: ${status.players.join(", ")}` : "")
    : "";
  const cpu = status.cpu_percent == null ? null : `${status.cpu_percent}%`;
  const memory = status.memory == null ? null
    : `${status.memory}${status.memory_percent == null ? "" : ` (${status.memory_percent}%)`}`;
  $("power-stats").replaceChildren(
    statItem("CPU", status.running ? cpu : null),
    statItem("Memory", status.running ? memory : null),
    statItem("Version", status.version),
    statItem("World size", status.world_size),
    statItem("Port", status.port),
    statItem("Started", status.running ? status.started : null),
  );
  $("power-start").hidden = status.running || status.state === "starting";
  $("power-restart").hidden = !status.running;
  $("power-stop").hidden = !status.running;
  $("power-kill").hidden = !(status.running || status.state === "starting");
}

async function loadStatus() {
  try {
    renderStatus(await api(`${base}/status`));
    $("power-error").textContent = "";
  } catch (error) {
    $("power-error").textContent = `Could not read the server status: ${error.message}`;
  }
}

async function sendPower(action, button) {
  if (POWER_CONFIRM[action] && !window.confirm(POWER_CONFIRM[action])) return;
  const buttons = ["start", "restart", "stop", "kill"].map((name) => $(`power-${name}`));
  buttons.forEach((node) => { node.disabled = true; });
  $("power-error").textContent = "";
  try {
    await api(`${base}/power`, { method: "POST", body: { action } });
    // Crafty starts and stops in the background; poll faster for a little while.
    for (const delay of [1500, 3000, 6000]) setTimeout(loadStatus, delay);
  } catch (error) {
    $("power-error").textContent = error.message;
  } finally {
    buttons.forEach((node) => { node.disabled = false; });
    button.blur();
  }
}

for (const action of ["start", "restart", "stop", "kill"]) {
  const button = $(`power-${action}`);
  button.addEventListener("click", () => sendPower(action, button));
}
loadStatus();
setInterval(() => { if (!document.hidden) loadStatus(); }, STATUS_REFRESH_MS);
