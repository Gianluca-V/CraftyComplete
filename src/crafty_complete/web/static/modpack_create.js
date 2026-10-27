"use strict";

const projectRef = $("create-page").dataset.project;
const form = $("create-form");

async function loadVersions() {
  try {
    const data = await api(`/api/modpacks/${encodeURIComponent(projectRef)}/versions`);
    $("pack-title").textContent = `Create a server from ${data.title}`;
    $("server-name").value = data.title.slice(0, 64);
    $("version").replaceChildren(...data.versions.map((v) => {
      const kind = v.version_type && v.version_type !== "release" ? ` (${v.version_type})` : "";
      const label = `${v.version_number}${kind} · ${v.loaders.join(", ")} · ${v.game_versions.join(", ")}`;
      const option = el("option", { value: v.id }, v.supported ? label : `${label} (not supported)`);
      option.disabled = !v.supported;
      return option;
    }));
    // Default to the newest supported release, else the newest supported version.
    const firstSupported = data.versions.find((v) => v.supported && v.version_type === "release")
      || data.versions.find((v) => v.supported);
    if (firstSupported) $("version").value = firstSupported.id;
    else $("form-error").textContent = "No version of this modpack uses Fabric, Forge or NeoForge.";
  } catch (error) {
    $("form-error").textContent = error.message;
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

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  $("form-error").textContent = "";
  const data = new FormData(form);
  $("create").disabled = true;
  try {
    const { job_id: jobId } = await api("/api/modpacks/servers", {
      method: "POST",
      body: {
        project_id: projectRef,
        version_id: data.get("version_id") || null,
        name: data.get("name").trim(),
        port: Number(data.get("port")),
        mem_min: Number(data.get("mem_min")),
        mem_max: Number(data.get("mem_max")),
        agree_to_eula: data.get("agree_to_eula") === "on",
      },
    });
    follow(jobId);
  } catch (error) {
    $("form-error").textContent = error.message;
    $("create").disabled = false;
  }
});

loadVersions();
