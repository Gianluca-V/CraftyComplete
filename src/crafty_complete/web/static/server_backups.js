"use strict";

// Backups panel of the server page. Loaded after server.js (uses `base`).

let backupsLoaded = false;
let backupConfigs = [];

// Crafty names backups with its own local time and no timezone: show it as written.
function backupDate(created) {
  return created ? created.replace("T", " ") : "Unknown date";
}

function renderBackups() {
  const select = $("backup-config");
  select.replaceChildren(...backupConfigs.map((config) =>
    el("option", { value: config.backup_id },
      config.name + (config.stops_server ? " (stops the server)" : ""))));
  const preferred = backupConfigs.find((config) => config.default) || backupConfigs[0];
  if (preferred) select.value = preferred.backup_id;
  $("backup-config-field").hidden = backupConfigs.length < 2;
  $("backup-now").disabled = backupConfigs.length === 0;

  $("backup-lists").replaceChildren(...backupConfigs.map((config) => {
    const items = config.backups.map((backup) => el("li", {},
      el("span", { class: "backup-date" }, backupDate(backup.created)),
      el("span", { class: "muted" }, backup.file)));
    const empty = config.type === "zip_vault"
      ? "No backups yet."
      : "Crafty does not list snapshot backups here; see them in the Crafty panel.";
    return el("section", { class: "backup-config" },
      backupConfigs.length > 1 ? el("h3", {}, config.name) : null,
      el("ul", { class: "player-list backup-list" },
        ...(items.length ? items : [el("li", { class: "empty" }, empty)])));
  }));
  $("backups-info").textContent = backupConfigs.length
    ? "Dates are in the Crafty server's local time."
    : "This server has no backup configuration in Crafty; create one in the Crafty panel.";
}

async function loadBackups() {
  try {
    backupConfigs = (await api(`${base}/backups`)).configs;
    $("backups-error").textContent = "";
    renderBackups();
    backupsLoaded = true;
  } catch (error) {
    $("backups-error").textContent = `Could not list the backups: ${error.message}`;
  }
}

async function backupNow() {
  const config = backupConfigs.find((c) => c.backup_id === $("backup-config").value);
  if (!config) return;
  if (config.stops_server && !window.confirm(
    `The "${config.name}" backup stops the server while it runs. Back up now?`)) return;
  $("backup-now").disabled = true;
  $("backups-error").textContent = "";
  try {
    await api(`${base}/backups`, { method: "POST", body: { backup_id: config.backup_id } });
    $("backups-info").textContent =
      "Crafty is making the backup. It appears in the list when it finishes; press Refresh.";
  } catch (error) {
    $("backups-error").textContent = `Backup not started: ${error.message}`;
  } finally {
    $("backup-now").disabled = backupConfigs.length === 0;
  }
}

$("backup-now").addEventListener("click", backupNow);
$("backups-refresh").addEventListener("click", loadBackups);
// List the backups only when someone opens the panel.
$("backups-panel").addEventListener("toggle", () => {
  if ($("backups-panel").open && !backupsLoaded) loadBackups();
});
