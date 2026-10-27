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
          el("span", { class: "chevron", "aria-hidden": "true" }, "›")))));
    status.textContent = servers.length ? "" : "Your Crafty account cannot see any servers.";
  } catch (error) {
    status.textContent = `Could not load servers: ${error.message}`;
    status.classList.add("is-error");
  }
}

loadServers();
