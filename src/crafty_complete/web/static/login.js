"use strict";

const form = document.getElementById("login-form");
const errorBox = document.getElementById("login-error");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.textContent = "";
  const data = new FormData(form);
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;

  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: data.get("username").trim(),
        password: data.get("password"),
        totp: data.get("totp").trim() || null,
      }),
    });
    if (response.ok) {
      window.location.assign("/");
      return;
    }
    const body = await response.json().catch(() => ({}));
    errorBox.textContent = typeof body.detail === "string"
      ? body.detail
      : "Fill in your username and password.";
  } catch {
    errorBox.textContent = "Could not reach craftyComplete.";
  } finally {
    submit.disabled = false;
  }
});
