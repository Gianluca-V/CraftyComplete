"use strict";

const serverId = $("server-page").dataset.serverId;
const base = `/api/servers/${encodeURIComponent(serverId)}`;
// Updates available (excluding pinned mods) in the last render.
let updateCount = 0;

function setStatus(message, isError = false) {
  $("status").textContent = message;
  $("status").classList.toggle("is-error", isError);
}

function badge(text, kind) {
  return el("span", { class: `badge ${kind}` }, text);
}

function statusBadges(mod) {
  const badges = [];
  if (mod.state === "unknown") badges.push(badge("Not on Modrinth", "muted-badge"));
  else if (mod.pinned) badges.push(badge("Pinned", "pin-badge"));
  else if (mod.update_available) badges.push(badge("Update available", "update-badge"));
  else badges.push(badge("Up to date", "ok-badge"));
  if (mod.server_side === "unsupported") badges.push(badge("Client only", "warn-badge"));
  return badges;
}

function renderMod(mod) {
  const icon = el("img", { class: "mod-icon", alt: "", width: "32", height: "32", loading: "lazy" });
  const iconUrl = safeUrl(mod.icon_url);
  if (iconUrl) icon.src = iconUrl;

  const name = mod.slug
    ? el("a", { href: `https://modrinth.com/mod/${encodeURIComponent(mod.slug)}`,
                target: "_blank", rel: "noopener" }, mod.title || mod.slug)
    : el("span", {}, mod.title || mod.file_name);

  const version = el("td", { class: "version-cell" }, mod.version_number || "—");
  if (mod.update_available && !mod.pinned) {
    version.append(" → ", el("strong", {}, mod.latest_version_number));
  }

  const pin = el("button", {
    class: "ghost compact",
    "aria-pressed": String(mod.pinned),
    title: mod.pinned ? "Allow updates for this mod" : "Never update this mod",
  }, mod.pinned ? "Unpin" : "Pin");
  if (mod.state === "unknown") {
    pin.disabled = true;
    pin.dataset.unknown = "true";
  }
  pin.addEventListener("click", () => togglePin(mod, pin));

  const actions = el("td", { class: "pin-cell" });
  if (mod.update_available && !mod.pinned) {
    const update = el("button", { class: "primary compact" }, "Update");
    update.addEventListener("click", () =>
      runAction(update, `${base}/mods/${encodeURIComponent(mod.file_name)}/update`,
                `Updating ${mod.title || mod.file_name}…`));
    actions.append(update, " ");
  }
  actions.append(pin);

  return el("tr", {},
    el("td", {},
      el("div", { class: "mod-name" }, icon,
        el("div", {}, name, el("div", { class: "file-name" }, mod.file_name)))),
    version,
    el("td", {}, el("div", { class: "badges" }, ...statusBadges(mod))),
    actions);
}

function render(data) {
  $("scan").textContent = data.adopted ? "Rescan" : "Adopt server";
  if (data.loader) $("loader").value = data.loader;
  if (data.game_version) $("game-version").value = data.game_version;

  $("modpack-panel").hidden = !data.adopted;
  $("check-updates").hidden = !data.adopted;
  $("update-all").hidden = !data.adopted;
  updateCount = data.counts.updates;
  $("update-all").disabled = !updateCount;
  $("update-all").textContent = data.counts.updates ? `Update all (${data.counts.updates})` : "Update all";

  if (!data.adopted) {
    $("scan-info").textContent = "Not adopted yet. Adopting reads the mods folder and identifies each jar on Modrinth.";
    $("summary").hidden = true;
    $("mod-table").hidden = true;
    return;
  }
  const detected = [data.loader, data.game_version].filter(Boolean).join(" ");
  $("scan-info").textContent =
    `${detected || "Loader and version unknown"} · scanned ${relativeDate(data.scanned_at)}` +
    (data.updates_checked_at ? ` · updates checked ${relativeDate(data.updates_checked_at)}` : "") +
    (data.modpack ? ` · from ${data.modpack.title} ${data.modpack.version_number}` : "");
  const c = data.counts;
  $("summary").replaceChildren(
    el("span", {}, el("strong", {}, String(c.identified)), " identified"),
    el("span", {}, el("strong", {}, String(c.unknown)), " unknown"),
    el("span", { class: c.updates ? "has-updates" : "" },
      el("strong", {}, String(c.updates)), c.updates === 1 ? " update available" : " updates available"),
    el("span", {}, el("strong", {}, String(c.pinned)), " pinned"));
  $("summary").hidden = false;
  $("mod-rows").replaceChildren(...data.mods.map(renderMod));
  $("mod-table").hidden = data.mods.length === 0;
  if (!data.mods.length) setStatus("The mods folder has no .jar files.");
}

function setBusy(busy) {
  document.querySelectorAll("#server-page button").forEach((b) => { b.disabled = busy; });
  if (!busy) {
    $("update-all").disabled = updateCount === 0;
    $("modpack-link").disabled = $("modpack-link-version").disabled;
    document.querySelectorAll("#mod-rows button[data-unknown]").forEach((b) => { b.disabled = true; });
  }
}

/** POST an install/update action, show its outcome and re-render the table. */
async function runAction(button, path, message) {
  setBusy(true);
  setStatus(message);
  try {
    const outcome = await api(path, { method: "POST", body: {} });
    setStatus("");
    showOutcome($("notice"), outcome);
    render(outcome.server);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function checkUpdates() {
  setBusy(true);
  setStatus("Checking Modrinth for updates…");
  try {
    render(await api(`${base}/check-updates`, { method: "POST", body: {} }));
    setStatus("");
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function togglePin(mod, button) {
  button.disabled = true;
  try {
    await api(`${base}/mods/${encodeURIComponent(mod.file_name)}`, {
      method: "PATCH", body: { pinned: !mod.pinned },
    });
    render(await api(`${base}/mods`));
  } catch (error) {
    setStatus(error.message, true);
    button.disabled = false;
  }
}

async function scan() {
  const button = $("scan");
  button.disabled = true;
  setStatus("Scanning mods… large servers can take a minute.");
  try {
    const data = await api(`${base}/scan`, {
      method: "POST",
      body: {
        loader: $("loader").value || null,
        game_version: $("game-version").value || null,
      },
    });
    setStatus("");
    render(data);
    await loadModpack();
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function loadServerName() {
  try {
    const servers = await api("/api/servers");
    const server = servers.find((s) => s.server_id === serverId);
    if (server) {
      $("server-name").textContent = server.server_name;
      document.title = `craftyComplete — ${server.server_name}`;
    }
  } catch {
    // The name is cosmetic; the mods call below reports real errors.
  }
}

async function loadGameVersions() {
  try {
    const filters = await api("/api/catalog/filters?type=mod");
    $("game-version").append(...filters.game_versions.map((v) => el("option", { value: v }, v)));
  } catch {
    // Without the list the user can still scan with detection.
  }
}

// ---- Modpack: link, update, follow the job ----

function versionLabel(v) {
  return `${v.version_number}${v.version_type && v.version_type !== "release" ? ` (${v.version_type})` : ""}`;
}

async function loadModpack() {
  let status;
  try {
    status = await api(`${base}/modpack`);
  } catch (error) {
    $("modpack-info").textContent = error.message;
    return;
  }
  $("modpack-linked").hidden = !status.linked;
  $("modpack-link-form").hidden = Boolean(status.linked);
  if (!status.linked) return;
  const { linked, latest } = status;
  $("modpack-info").replaceChildren(
    el("strong", {}, linked.title), ` ${linked.version_number}`,
    status.update_available
      ? el("span", { class: "badge update-badge" }, `Update available: ${latest.version_number}`)
      : el("span", { class: "badge ok-badge" }, "Up to date"));
  const others = status.versions.filter((v) => v.id !== linked.version_id);
  $("modpack-version").replaceChildren(
    ...others.map((v) => el("option", { value: v.id }, versionLabel(v))));
  if (latest && latest.id !== linked.version_id) $("modpack-version").value = latest.id;
  $("modpack-update").disabled = others.length === 0;
}

/** Accepts "prominence-2" or "https://modrinth.com/modpack/prominence-2/versions". */
function projectRef(text) {
  const value = text.trim();
  const match = value.match(/modrinth\.com\/modpack\/([A-Za-z0-9_.-]+)/);
  return match ? match[1] : value;
}

async function findVersions() {
  const select = $("modpack-link-version");
  select.disabled = true;
  $("modpack-link").disabled = true;
  setStatus("Looking up the modpack on Modrinth…");
  try {
    const data = await api(`/api/modpacks/${encodeURIComponent(projectRef($("modpack-ref").value))}/versions`);
    const loader = $("loader").value;
    const game = $("game-version").value;
    // The server's Minecraft version and loader narrow the list; the link checks them again.
    const fits = data.versions.filter((v) =>
      (!game || v.game_versions.includes(game)) && (!loader || v.loaders.includes(loader)));
    const shown = fits.length ? fits : data.versions;
    select.replaceChildren(...shown.map((v) => el("option", { value: v.id }, versionLabel(v))));
    select.dataset.projectId = data.project_id;
    select.disabled = shown.length === 0;
    $("modpack-link").disabled = shown.length === 0;
    setStatus(shown.length ? `${data.title}: pick the version this server runs.`
      : `${data.title} has no versions.`, !shown.length);
  } catch (error) {
    setStatus(error.message, true);
  }
}

function renderJob(job) {
  $("job").hidden = false;
  $("job-title").textContent = job.title;
  const progress = job.progress.total ? ` (${job.progress.done}/${job.progress.total})` : "";
  $("job-step").textContent = job.status === "running" ? `${job.step}${progress}…`
    : job.status === "succeeded" ? "Done." : "Failed.";
  $("job-progress").max = job.progress.total || 1;
  $("job-progress").value = job.status === "succeeded" ? $("job-progress").max : job.progress.done;
  $("job-warnings").replaceChildren(...job.warnings.map((w) => el("li", {}, w)));
  $("job-error").textContent = job.error || "";
  $("job-result").replaceChildren(...describeResult(job.result));
}

function describeResult(result) {
  if (!result) return [];
  const parts = [];
  if ("added" in result) {
    parts.push(`${result.added.length} added, ${result.updated.length} updated, ` +
      `${result.removed.length} removed. Old files are in ${result.backup_folder}. `);
    if (result.own_mods_kept.length) parts.push(`Kept your own mods: ${result.own_mods_kept.join(", ")}. `);
    if (result.restart_required) parts.push(el("strong", {}, "Start the server in Crafty to apply the update."));
  } else {
    if (result.own_mods.length) parts.push(`Mods you added yourself: ${result.own_mods.join(", ")}. `);
    if (result.missing.length) parts.push(`Pack files not on the server: ${result.missing.join(", ")}.`);
  }
  return parts;
}

async function startJob(path, body) {
  setBusy(true);
  setStatus("");
  try {
    const { job_id: jobId } = await api(path, { method: "POST", body });
    while (true) {
      const job = await api(`/api/jobs/${jobId}`);
      renderJob(job);
      if (job.status !== "running") break;
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    render(await api(`${base}/mods`));
    await loadModpack();
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function init() {
  $("modpack-find").addEventListener("click", findVersions);
  $("modpack-link-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const select = $("modpack-link-version");
    startJob(`${base}/modpack/link`, { project_id: select.dataset.projectId, version_id: select.value });
  });
  $("modpack-update").addEventListener("click", () =>
    startJob(`${base}/modpack/update`, { version_id: $("modpack-version").value }));
  $("scan").addEventListener("click", scan);
  $("check-updates").addEventListener("click", checkUpdates);
  $("update-all").addEventListener("click", () =>
    runAction($("update-all"), `${base}/update-all`, "Updating every unpinned mod…"));
  await Promise.all([loadServerName(), loadGameVersions()]);
  try {
    const data = await api(`${base}/mods`);
    render(data);
    if (data.adopted) await loadModpack();
  } catch (error) {
    setStatus(error.message, true);
    $("scan").disabled = true;
  }
}

init();
