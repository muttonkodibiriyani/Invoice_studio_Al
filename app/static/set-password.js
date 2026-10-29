// The create-password / reset-password page. The code arrives in the URL fragment (never sent to the server),
// is verified with the identity provider, and is consumed when the new password is saved.
(() => {
  const $ = (selector) => document.querySelector(selector);
  const MIN_LENGTH = 12;
  const RETRY_DELAYS = [2000, 4000, 8000, 15000];
  const FINAL_ERRORS = new Set([
    "auth/expired-action-code", "auth/invalid-action-code", "auth/user-disabled", "auth/weak-password",
  ]);

  function codeFromLocation() {
    const fragment = new URLSearchParams(location.hash.replace(/^#/, ""));
    return fragment.get("oobCode") || "";
  }

  function show({ message, detail = "", busy = false, form = false, signIn = false, retry = false, focus = false }) {
    $("#sp-message").textContent = message;
    $("#sp-spinner").hidden = !busy;
    $("#sp-form").hidden = !form;
    $("#sp-sign-in").hidden = !signIn;
    $("#sp-retry").hidden = !retry;
    $("#sp-detail").hidden = !detail;
    $("#sp-detail").textContent = detail;
    if (focus) $("#sp-message").focus();
  }

  function friendly(error) {
    const code = String(error?.code || "");
    if (code.includes("weak-password")) return "Choose a stronger password (at least 12 characters).";
    if (code.includes("expired-action-code") || code.includes("invalid-action-code")) return "This link has expired or was already used. Ask your workspace administrator for a new link.";
    if (code.includes("user-disabled")) return "This account is disabled. Contact your workspace administrator.";
    if (code.includes("network-request-failed")) return "The sign-in service could not be reached. Check the connection and try again.";
    return "The password could not be saved. Ask your workspace administrator for a new link.";
  }

  async function authModule(method) {
    if (typeof window.InvoiceStudioAuth?.[method] === "function") return window.InvoiceStudioAuth;
    // Retry a failed deferred bundle load using only a fixed same-origin external script.
    // Neither the action code nor the password is attached to this element or its URL.
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/static/firebase-auth.bundle.js";
      script.async = true;
      let settled = false;
      const finish = (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        script.remove();
        if (error) reject(error);
        else resolve();
      };
      const timeout = setTimeout(() => finish(new Error("Sign-in module timed out")), 15000);
      script.addEventListener("load", () => finish(
        typeof window.InvoiceStudioAuth?.[method] === "function" ? null : new Error("Sign-in module unavailable"),
      ), { once: true });
      script.addEventListener("error", () => finish(new Error("Sign-in module unavailable")), { once: true });
      document.head.append(script);
    });
    return window.InvoiceStudioAuth;
  }

  async function init() {
    const code = codeFromLocation();
    if (location.hash || location.search) history.replaceState(null, "", location.pathname);
    if (!code) {
      show({ message: "This link is incomplete.", detail: "Open the full link you were given, or ask your workspace administrator for a new one.", focus: true });
      return;
    }
    let email = "";
    let busy = false;
    let retryAction = null;

    async function withRetries(method, operation, onSuccess, retry, saving = false) {
      if (busy) return;
      busy = true;
      retryAction = null;
      const submit = $("#sp-submit");
      submit.disabled = true;
      $("#sp-retry").disabled = true;
      submit.textContent = saving ? "Saving…" : "Save password";
      show({ message: saving ? "Saving your password…" : "Checking your link…", busy: true });
      try {
        for (let attempt = 0; ; attempt++) {
          let result;
          try {
            const auth = await authModule(method);
            result = await operation(auth);
          } catch (error) {
            if (FINAL_ERRORS.has(String(error?.code || ""))) {
              show({
                message: saving ? "The password could not be saved." : "This link cannot be used.",
                detail: friendly(error),
                form: saving && error.code === "auth/weak-password",
                focus: true,
              });
              return;
            }
            if (attempt === RETRY_DELAYS.length) {
              retryAction = retry;
              show({
                message: "The sign-in service could not be reached.",
                detail: "The workspace may still be starting. Check the connection, then try again here.",
                retry: true, form: saving, focus: true,
              });
              return;
            }
            const delay = RETRY_DELAYS[attempt];
            show({
              message: saving ? "Reconnecting to save your password…" : "Starting the sign-in service…",
              detail: `The workspace may be starting. Trying again in ${delay / 1000} seconds.`,
              busy: true, focus: true,
            });
            await new Promise((resolve) => setTimeout(resolve, delay));
            continue;
          }
          onSuccess(result);
          return;
        }
      } finally {
        busy = false;
        submit.disabled = false;
        $("#sp-retry").disabled = false;
        submit.textContent = "Save password";
      }
    }

    async function verifyLink() {
      await withRetries("verifyResetCode", (auth) => auth.verifyResetCode(code), (verifiedEmail) => {
        email = verifiedEmail;
        $("#sp-email").value = email;
        show({ message: "Create your password", form: true });
        $("#sp-password").focus();
      }, verifyLink);
    }

    async function savePassword() {
      if (busy) return;
      const password = $("#sp-password").value;
      const confirm = $("#sp-confirm").value;
      if (password.length < MIN_LENGTH) {
        show({ message: "Create your password", detail: `Use at least ${MIN_LENGTH} characters.`, form: true, focus: true });
        return;
      }
      if (password !== confirm) {
        show({ message: "Create your password", detail: "The two passwords do not match.", form: true, focus: true });
        return;
      }
      await withRetries("confirmReset", (auth) => auth.confirmReset(code, password), () => {
        $("#sp-password").value = "";
        $("#sp-confirm").value = "";
        show({ message: "Your password is set.", detail: `Sign in as ${email} to open Invoice Studio.`, signIn: true });
        $("#sp-sign-in").focus();
      }, savePassword, true);
    }

    $("#sp-form").addEventListener("submit", (event) => {
      event.preventDefault();
      return savePassword();
    });
    $("#sp-retry").addEventListener("click", () => {
      if (retryAction && !busy) return retryAction();
    });
    await verifyLink();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
