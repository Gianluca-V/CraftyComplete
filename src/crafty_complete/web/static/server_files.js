"use strict";

// Files panel of the server page: browse folders and edit text files. Loaded after
// server.js (uses `base`).

let filesLoaded = false;
// The open file: its path, the content as opened or last saved, and that content's hash.
let openFile = null;

function fileDirty() {
  return openFile !== null && $("file-text").value !== openFile.content;
}

function confirmDiscard() {
  return !fileDirty() || window.confirm(`Discard your unsaved changes to ${openFile.path}?`);
}

function showFilesError(message) {
  $("files-error").textContent = message;
}

function renderCrumbs(path) {
  const crumbs = [["Server folder", ""]];
  const parts = path ? path.split("/") : [];
  parts.forEach((part, index) => crumbs.push([part, parts.slice(0, index + 1).join("/")]));
  $("files-crumbs").replaceChildren(...crumbs.map(([label, target], index) => {
    if (index === crumbs.length - 1) return el("span", { "aria-current": "location" }, label);
    const link = el("button", { class: "crumb", type: "button" }, label);
    link.addEventListener("click", () => loadFolder(target));
    return el("span", {}, link, " / ");
  }));
}

async function loadFolder(path) {
  showFilesError("");
  try {
    const data = await api(`${base}/files?path=${encodeURIComponent(path)}`);
    renderCrumbs(data.path);
    const items = data.entries.map((entry) => {
      const button = el("button", { class: "file-entry", type: "button" },
        el("span", { class: "file-icon", "aria-hidden": "true" }, entry.is_dir ? "▸" : "·"),
        entry.is_dir ? `${entry.name}/` : entry.name);
      button.addEventListener("click", () =>
        entry.is_dir ? loadFolder(entry.path) : openTextFile(entry.path));
      return el("li", {}, button);
    });
    $("files-list").replaceChildren(
      ...(items.length ? items : [el("li", { class: "empty" }, "This folder is empty.")]));
    filesLoaded = true;
  } catch (error) {
    showFilesError(`Could not list the folder: ${error.message}`);
  }
}

function setEditorState(message) {
  $("file-editor-state").textContent = message;
}

function refreshDirty() {
  $("file-save").disabled = !fileDirty();
  setEditorState(fileDirty() ? "Unsaved changes" : "");
}

async function openTextFile(path) {
  if (!confirmDiscard()) return;
  showFilesError("");
  try {
    const data = await api(`${base}/files/content?path=${encodeURIComponent(path)}`);
    $("file-editor-path").textContent = data.path;
    $("file-text").value = data.content;
    // Textareas turn "\r\n" into "\n"; compare against what it holds, not the raw file.
    openFile = { path: data.path, content: $("file-text").value, sha256: data.sha256 };
    $("file-editor").hidden = false;
    refreshDirty();
    $("file-text").focus();
  } catch (error) {
    showFilesError(error.message);
  }
}

async function saveFile() {
  if (!fileDirty()) return;
  const content = $("file-text").value;
  $("file-save").disabled = true;
  setEditorState("Saving…");
  try {
    const result = await api(`${base}/files/content`, {
      method: "PUT", body: { path: openFile.path, content, sha256: openFile.sha256 },
    });
    openFile = { ...openFile, content, sha256: result.sha256 };
    showFilesError("");
    refreshDirty();
    setEditorState("Saved");
  } catch (error) {
    refreshDirty();
    showFilesError(`Not saved: ${error.message}`);
  }
}

function closeFile() {
  if (!confirmDiscard()) return;
  openFile = null;
  $("file-editor").hidden = true;
  $("file-text").value = "";
  showFilesError("");
}

$("file-text").addEventListener("input", refreshDirty);
$("file-text").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    saveFile();
  }
});
$("file-save").addEventListener("click", saveFile);
$("file-close").addEventListener("click", closeFile);
window.addEventListener("beforeunload", (event) => {
  if (fileDirty()) event.preventDefault();
});
// List the server folder only when someone opens the panel.
$("files-panel").addEventListener("toggle", () => {
  if ($("files-panel").open && !filesLoaded) loadFolder("");
});
