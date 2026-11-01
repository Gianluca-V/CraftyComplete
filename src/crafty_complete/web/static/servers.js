"use strict";

async function loadServers() {
  const status = $("status");
  status.textContent = "Loading servers…";
  try {
    const servers = await api("/api/servers");
    $("server-list").replaceChildren(...servers.map((server) =>
      el("li", {},
        el("a", { class: "server-row", href: `/servers/${encodeURIComponent(server.server_id)}` },
          el("span", { class: "server-name" }, server.server_name),
          el("span", { class: "muted" }, server.type || ""),
          el("span", { class: "badge muted-badge", "data-status-for": server.server_id }, "…"),
          el("span", { class: "chevron", "aria-hidden": "true" }, "›")))));
    status.textContent = servers.length ? "" : "Your Crafty account cannot see any servers.";
    servers.forEach((server) => loadState(server.server_id));
  } catch (error) {
    status.textContent = `Could not load servers: ${error.message}`;
    status.classList.add("is-error");
  }
}

async function loadState(serverId) {
  const node = document.querySelector(`[data-status-for="${CSS.escape(serverId)}"]`);
  try {
    const state = await api(`/api/servers/${encodeURIComponent(serverId)}/status`);
    node.textContent = state.running
      ? `Running · ${state.players_online}/${state.max_players}`
      : state.state.charAt(0).toUpperCase() + state.state.slice(1);
    node.className = `badge ${state.running ? "running-badge"
      : state.state === "crashed" ? "crashed-badge" : "muted-badge"}`;
  } catch {
    node.textContent = "Status unavailable";
  }
}

loadServers();
