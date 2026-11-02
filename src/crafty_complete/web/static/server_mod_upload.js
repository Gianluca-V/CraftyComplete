"use strict";

// Drop zone of the server page: upload jars into mods/. Loaded after server.js (uses `base`,
// `render`, `setBusy`).

const MAX_JAR_BYTES = 90 * 1024 * 1024;

function uploadResult(text, kind) {
  $("upload-results").append(el("li", { class: kind }, text));
}

/** Upload one jar; resolves to true when it reached the server. */
async function uploadJar(file) {
  if (!file.name.toLowerCase().endsWith(".jar")) {
    uploadResult(`${file.name}: only .jar files can be added to mods/.`, "error");
    return false;
  }
  if (file.size > MAX_JAR_BYTES) {
    uploadResult(`${file.name}: larger than 90 MiB; upload it in Crafty instead.`, "error");
    return false;
  }
  const response = await fetch(
    `${base}/mods/upload?file_name=${encodeURIComponent(file.name)}`,
    { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
  if (redirectIfLoggedOut(response)) return false;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`;
    uploadResult(`${file.name}: ${detail}`, "error");
    return false;
  }
  uploadResult(`Added ${body.title ? `${body.title} (${body.file_name})` : body.file_name}`, "ok");
  for (const warning of body.warnings) uploadResult(warning, "warn");
  return true;
}

/** Upload the files one after another, then refresh the mods table once. */
async function uploadJars(files) {
  if (!files.length) return;
  $("upload-results").replaceChildren();
  setBusy(true);
  setStatus(`Uploading ${files.length} file${files.length === 1 ? "" : "s"}…`);
  let added = 0;
  try {
    for (const file of files) {
      try {
        if (await uploadJar(file)) added += 1;
      } catch (error) {
        uploadResult(`${file.name}: ${error.message}`, "error");
      }
    }
    if (added) {
      render(await api(`${base}/mods`));
      uploadResult("Changed mods load the next time the server starts.", "muted");
    }
    setStatus("");
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setBusy(false);
  }
}

const drop = $("mod-drop");
drop.addEventListener("dragover", (event) => {
  event.preventDefault();
  drop.classList.add("is-over");
});
drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
drop.addEventListener("drop", (event) => {
  event.preventDefault();
  drop.classList.remove("is-over");
  uploadJars([...event.dataTransfer.files]);
});
$("mod-pick").addEventListener("click", () => $("mod-file").click());
$("mod-file").addEventListener("change", () => {
  uploadJars([...$("mod-file").files]);
  $("mod-file").value = "";
});
