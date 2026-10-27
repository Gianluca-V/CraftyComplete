"use strict";

const serverId = $("server-page").dataset.serverId;
const base = `/api/servers/${encodeURIComponent(serverId)}`;

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
  if (mod.state === "unknown") pin.disabled = true;
  pin.addEventListener("click", () => togglePin(mod, pin));

  return el("tr", {},
    el("td", {},
      el("div", { class: "mod-name" }, icon,
        el("div", {}, name, el("div", { class: "file-name" }, mod.file_name)))),
    version,
    el("td", {}, el("div", { class: "badges" }, ...statusBadges(mod))),
    el("td", { class: "pin-cell" }, pin));
}

function render(data) {
  $("scan").textContent = data.adopted ? "Rescan" : "Adopt server";
  if (data.loader) $("loader").value = data.loader;
  if (data.game_version) $("game-version").value = data.game_version;

  if (!data.adopted) {
    $("scan-info").textContent = "Not adopted yet. Adopting reads the mods folder and identifies each jar on Modrinth.";
    $("summary").hidden = true;
    $("mod-table").hidden = true;
    return;
  }
  const detected = [data.loader, data.game_version].filter(Boolean).join(" ");
  $("scan-info").textContent =
    `${detected || "Loader and version unknown"} · scanned ${relativeDate(data.scanned_at)}`;
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

async function init() {
  $("scan").addEventListener("click", scan);
  await Promise.all([loadServerName(), loadGameVersions()]);
  try {
    render(await api(`${base}/mods`));
  } catch (error) {
    setStatus(error.message, true);
    $("scan").disabled = true;
  }
}

init();
