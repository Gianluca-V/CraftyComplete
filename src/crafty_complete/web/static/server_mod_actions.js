"use strict";

// Version picker and removal dialogs of the server page. Loaded after server.js
// (uses `base`, `render`, `setBusy`, `setStatus`).

const dialog = $("mod-dialog");
// What the confirm button does for the dialog currently open.
let confirmAction = null;

function openDialog(title, confirmLabel, body, onConfirm) {
  $("mod-dialog-title").textContent = title;
  $("mod-dialog-body").replaceChildren(...body);
  $("mod-dialog-error").textContent = "";
  $("mod-dialog-confirm").textContent = confirmLabel;
  $("mod-dialog-confirm").disabled = false;
  confirmAction = onConfirm;
  dialog.showModal();
}

$("mod-dialog-form").addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "confirm" || !confirmAction) return;
  event.preventDefault();
  $("mod-dialog-confirm").disabled = true;
  $("mod-dialog-error").textContent = "";
  try {
    await confirmAction();
    dialog.close();
  } catch (error) {
    $("mod-dialog-error").textContent = error.message;
    $("mod-dialog-confirm").disabled = false;
  }
});

/** POST a mod action from a dialog; errors stay in the dialog. */
async function postFromDialog(path, body, message) {
  setBusy(true);
  setStatus(message);
  try {
    const outcome = await api(path, { method: "POST", body });
    setStatus("");
    showOutcome($("notice"), outcome);
    render(outcome.server);
  } catch (error) {
    setStatus("");
    throw error;
  } finally {
    setBusy(false);
  }
}

async function openVersionDialog(mod) {
  const title = mod.title || mod.file_name;
  const path = `${base}/mods/${encodeURIComponent(mod.file_name)}`;
  let data;
  try {
    data = await api(`${path}/versions`);
  } catch (error) {
    setStatus(error.message, true);
    return;
  }
  const select = el("select", { id: "mod-version-select", "aria-label": "Version" },
    ...data.versions.map((v) => {
      const label = `${v.version_number}${v.version_type === "release" ? "" : ` (${v.version_type})`}`
        + (v.id === data.current_version_id ? " — installed" : "");
      const option = el("option", { value: v.id }, label);
      if (v.id === data.current_version_id) option.disabled = true;
      return option;
    }));
  const choice = data.versions.find((v) => v.id !== data.current_version_id);
  if (choice) select.value = choice.id;
  const intro = el("p", { class: "muted" },
    "Versions for this server's loader and Minecraft version. The chosen version is not pinned, "
    + "so Update all can move it to the newest one.");
  openDialog(`Change ${title} version`, "Install version",
    choice ? [intro, select] : [el("p", {}, "No other compatible version is available.")],
    () => postFromDialog(`${path}/version`, { version_id: select.value },
                         `Installing ${title} ${select.selectedOptions[0]?.textContent || ""}…`));
  $("mod-dialog-confirm").disabled = !choice;
}

async function openRemoveDialog(mod) {
  const title = mod.title || mod.file_name;
  const path = `${base}/mods/${encodeURIComponent(mod.file_name)}`;
  let plan;
  try {
    plan = await api(`${path}/removal`);
  } catch (error) {
    setStatus(error.message, true);
    return;
  }
  const body = [el("p", {}, `Delete ${plan.file_name} from the server's mods folder.`)];
  for (const warning of plan.warnings) body.push(el("p", { class: "warn" }, warning));
  let force = null;
  if (plan.required_by.length) {
    body.push(el("p", { class: "warn" },
      `Required by ${plan.required_by.map((m) => m.title).join(", ")}. `
      + "Those mods may fail to load without it."));
  }
  // Also when Modrinth could not be asked: the server then refuses without confirmation.
  if (plan.required_by.length || !plan.requirements_checked) {
    force = el("input", { type: "checkbox", id: "remove-force" });
    body.push(el("label", { class: "dialog-check", for: "remove-force" }, force, "Remove it anyway"));
  }
  const depBoxes = plan.unused_dependencies.map((dep) => {
    const box = el("input", { type: "checkbox", value: dep.file_name });
    body.push(el("label", { class: "dialog-check" }, box,
      `Also remove ${dep.title} (${dep.file_name}), installed as a dependency and no longer needed`));
    return box;
  });
  openDialog(`Remove ${title}?`, "Remove", body, () => {
    if (force && !force.checked) {
      return Promise.reject(new Error("Tick \"Remove it anyway\" to confirm."));
    }
    return postFromDialog(`${path}/remove`, {
      force: Boolean(force?.checked),
      remove_dependencies: depBoxes.filter((b) => b.checked).map((b) => b.value),
    }, `Removing ${title}…`);
  });
}

function dialogList(items) {
  return el("ul", { class: "dialog-list" }, ...items.map((text) => el("li", {}, text)));
}

/** Ask before Update all: which mods move to which version. */
function openUpdateAllDialog() {
  const updates = currentMods.filter((mod) => mod.update_available && !mod.pinned);
  if (!updates.length) return;
  const pinned = currentMods.filter((mod) => mod.update_available && mod.pinned).length;
  const body = [
    el("p", {}, `${updates.length} mod${updates.length === 1 ? "" : "s"} will be updated:`),
    dialogList(updates.map((mod) =>
      `${mod.title || mod.file_name}: ${mod.version_number || "?"} → ${mod.latest_version_number}`)),
    el("p", { class: "muted" },
      (pinned ? `${pinned} pinned mod${pinned === 1 ? " stays" : "s stay"} as they are. ` : "")
      + "New required dependencies are installed too. Changes load the next time the server starts."),
  ];
  openDialog("Update all mods?", `Update ${updates.length}`, body, async () => {
    // The table shows the progress; the dialog closes right away.
    runAction($("update-all"), `${base}/update-all`, "Updating every unpinned mod…");
  });
}

/** Ask before moving the server to another version of its modpack. */
function openModpackUpdateDialog() {
  const select = $("modpack-version");
  const target = select.selectedOptions[0];
  if (!target || !linkedModpack) return;
  const body = [
    el("p", {}, el("strong", {}, linkedModpack.title), ` ${linkedModpack.version_number} → `,
      el("strong", {}, target.textContent)),
    el("p", { class: "muted" },
      "Stop the server in Crafty first. The pack's files are replaced; replaced and removed "
      + "files are moved to .crafty-complete/backups on the server, and mods you added "
      + "yourself stay installed. This can take several minutes."),
  ];
  openDialog("Change modpack version?", "Change version", body, async () => {
    startJob(`${base}/modpack/update`, { version_id: target.value });
  });
}
