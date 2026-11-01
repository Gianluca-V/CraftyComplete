"use strict";

const form = $("create-form");
let kinds = [];

function selectedKind() {
  return kinds.find((k) => k.kind === $("kind").value);
}

function showVersions() {
  const kind = selectedKind();
  const versions = kind ? kind.versions : [];
  $("minecraft").replaceChildren(...versions.map((v) => el("option", { value: v }, v)));
  $("minecraft").disabled = versions.length === 0;
  $("mods-field").hidden = !(kind && kind.modded);
  $("form-error").textContent = kind && !versions.length
    ? `Crafty offers no ${kind.label} version right now.` : "";
}

async function loadOptions() {
  try {
    kinds = (await api("/api/servers/new/options")).kinds;
    $("kind").replaceChildren(...kinds.map((k) => el("option", { value: k.kind }, k.label)));
    showVersions();
  } catch (error) {
    $("form-error").textContent = error.message;
  }
}

/** "lithium" or "https://modrinth.com/mod/lithium/versions" -> "lithium". */
function modRef(line) {
  const text = line.trim();
  const match = text.match(/modrinth\.com\/(?:mod|plugin|datapack)\/([^/?#\s]+)/i);
  return match ? match[1] : text;
}

function renderJob(job) {
  $("job").hidden = false;
  $("job-title").textContent = job.title;
  const progress = job.progress.total ? ` (${job.progress.done}/${job.progress.total})` : "";
  $("job-step").textContent = job.status === "running" ? `${job.step}${progress}…`
    : job.status === "succeeded" ? "Done." : "Failed.";
  $("job-progress").max = job.progress.total || 1;
  $("job-progress").value = job.status === "succeeded" ? $("job-progress").max : job.progress.done;
  const installed = (job.result && job.result.installed) || [];
  $("job-installed").replaceChildren(...installed.map((m) => el("li", {},
    `Installed ${m.title} ${m.version_number}${m.dependency ? " (dependency)" : ""}`)));
  $("job-warnings").replaceChildren(...job.warnings.map((w) => el("li", {}, w)));
  $("job-error").textContent = job.error || "";
  if (job.status === "succeeded" && job.server_id) {
    $("job-link").href = `/servers/${encodeURIComponent(job.server_id)}`;
    $("job-link").hidden = false;
  }
}

async function follow(jobId) {
  while (true) {
    let job;
    try {
      job = await api(`/api/jobs/${jobId}`);
    } catch (error) {
      $("job-error").textContent = `Lost track of the job: ${error.message}`;
      return;
    }
    renderJob(job);
    if (job.status !== "running") {
      $("create").disabled = job.status === "succeeded";
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

$("kind").addEventListener("change", showVersions);
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  $("form-error").textContent = "";
  const data = new FormData(form);
  const kind = selectedKind();
  const mods = kind && kind.modded
    ? data.get("mods").split("\n").map(modRef).filter(Boolean) : [];
  $("create").disabled = true;
  try {
    const { job_id: jobId } = await api("/api/servers/new", {
      method: "POST",
      body: {
        kind: data.get("kind"),
        minecraft: data.get("minecraft"),
        name: data.get("name").trim(),
        port: Number(data.get("port")),
        mem_min: Number(data.get("mem_min")),
        mem_max: Number(data.get("mem_max")),
        mods,
        agree_to_eula: data.get("agree_to_eula") === "on",
      },
    });
    follow(jobId);
  } catch (error) {
    $("form-error").textContent = error.message;
    $("create").disabled = false;
  }
});

loadOptions();
