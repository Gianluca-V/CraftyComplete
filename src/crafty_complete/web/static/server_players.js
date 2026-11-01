"use strict";

// Players panel of the server page. Loaded after server.js (uses `base`).

const PLAYER_CONFIRM = {
  ban: (name) => `Ban ${name} from this server?`,
  kick: (name) => `Kick ${name} from this server?`,
  deop: (name) => `Remove ${name} as an operator?`,
};
let playersLoaded = false;

function playerItem(name, detail, actions) {
  const item = el("li", {}, el("span", { class: "player-name" }, name,
    detail ? el("span", { class: "muted" }, ` · ${detail}`) : null));
  for (const [label, action] of actions) {
    const button = el("button", { class: "ghost compact", type: "button" }, label);
    button.addEventListener("click", () => playerAction(action, name));
    item.append(button);
  }
  return item;
}

function fillList(id, items) {
  $(id).replaceChildren(...(items.length ? items : [el("li", { class: "empty" }, "None")]));
}

async function loadPlayers() {
  try {
    const data = await api(`${base}/players`);
    $("players-mode").textContent = data.running
      ? "The server is running: changes are sent as console commands."
      : "The server is stopped: changes are written to its player files and apply on the next start. Kick needs a running server.";
    fillList("players-online", data.online.map((name) =>
      playerItem(name, null, [["Kick", "kick"], ["Ban", "ban"]])));
    fillList("players-whitelist", data.whitelist.map((p) =>
      playerItem(p.name, null, [["Remove", "whitelist_remove"]])));
    fillList("players-ops", data.ops.map((p) =>
      playerItem(p.name, p.level ? `level ${p.level}` : null, [["Deop", "deop"]])));
    fillList("players-banned", data.banned.map((p) =>
      playerItem(p.name, p.reason, [["Pardon", "pardon"]])));
    $("players-error").textContent = "";
    playersLoaded = true;
  } catch (error) {
    $("players-error").textContent = `Could not read the players: ${error.message}`;
  }
}

async function playerAction(action, name, reason = null) {
  const confirmText = PLAYER_CONFIRM[action];
  if (confirmText && !window.confirm(confirmText(name))) return false;
  $("players-error").textContent = "";
  try {
    const result = await api(`${base}/players`, {
      method: "POST", body: { action, name, reason: reason || null },
    });
    if (!result.changed) $("players-error").textContent = `Nothing to change for ${result.name}.`;
    // Console commands apply asynchronously; give the server a moment before reading files.
    setTimeout(loadPlayers, result.applied === "command" ? 1500 : 0);
    return true;
  } catch (error) {
    $("players-error").textContent = error.message;
    return false;
  }
}

$("player-action").addEventListener("change", () => {
  $("player-reason-field").hidden = !["ban", "kick"].includes($("player-action").value);
});
$("player-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const action = $("player-action").value;
  const reason = ["ban", "kick"].includes(action) ? $("player-reason").value.trim() : null;
  if (await playerAction(action, $("player-name").value.trim(), reason)) {
    $("player-name").value = "";
    $("player-reason").value = "";
  }
});
// Read the player files only when someone opens the panel.
$("players-panel").addEventListener("toggle", () => {
  if ($("players-panel").open && !playersLoaded) loadPlayers();
});
