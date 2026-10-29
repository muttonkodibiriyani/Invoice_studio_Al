// Microsoft Entra ID sign-in for the Azure build (design doc "Entra ID sign-in design" §4.1): a public SPA client
// with the auth code flow and PKCE, redirect only. The server hands out public values (tenant, client id, scope);
// there is no secret here. app.js only sees window.InvoiceStudioAuth.
import { CacheLookupPolicy, InteractionRequiredAuthError, PublicClientApplication } from "@azure/msal-browser";

const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const NOT_ASSIGNED = "This account has not been granted access to this workspace. Ask your administrator to add you to the Invoice Studio group.";

let runtime = null;
let client = null;
let account = null;
let scopes = [];

function requireEntraConfig(config) {
  const entra = config?.entra;
  if (!entra || !GUID.test(entra.clientId || "") || !GUID.test(entra.tenantId || "")
      || entra.authority !== `https://login.microsoftonline.com/${entra.tenantId}`
      || !Array.isArray(entra.scopes) || !entra.scopes.length) {
    throw new Error("Cloud sign-in is not configured completely. Contact the workspace administrator.");
  }
  return entra;
}

async function readPublicConfig() {
  let response;
  try {
    response = await fetch("/api/public-config", { headers: { Accept: "application/json" }, cache: "no-store" });
  } catch (_) {
    throw new Error("The workspace configuration service could not be reached.");
  }
  if (!response.ok) throw new Error("The workspace configuration could not be verified.");
  const config = await response.json();
  if (typeof config.cloud !== "boolean") throw new Error("The workspace returned an invalid access configuration.");
  return config;
}

// What Entra said when it sent the browser back without a token. Nothing else from the error reaches the page.
function redirectErrorMessage(error) {
  const text = `${error?.errorCode || ""} ${error?.errorMessage || ""}`;
  if (text.includes("AADSTS50105")) return NOT_ASSIGNED;
  if (text.includes("user_cancelled") || text.includes("access_denied")) return "Sign-in was cancelled.";
  return "Microsoft sign-in could not be completed. Try again.";
}

function publicUser(entry) {
  return entry ? { email: entry.username || "", name: entry.name || "" } : null;
}

async function bootstrap() {
  runtime = await readPublicConfig();
  if (!runtime.cloud) return { cloud: false, user: null };
  const entra = requireEntraConfig(runtime);
  scopes = entra.scopes;
  const home = `${location.origin}/`;
  client = new PublicClientApplication({
    auth: { clientId: entra.clientId, authority: entra.authority, redirectUri: home, postLogoutRedirectUri: home },
    cache: { cacheLocation: "sessionStorage" },
  });
  await client.initialize();
  let error = "";
  try {
    const result = await client.handleRedirectPromise();
    if (result?.account) client.setActiveAccount(result.account);
  } catch (redirectError) {
    error = redirectErrorMessage(redirectError);
  }
  // Only an account from this workspace's own tenant is used.
  account = client.getActiveAccount() || client.getAllAccounts().find((entry) => entry.tenantId === entra.tenantId) || null;
  if (account?.tenantId !== entra.tenantId) account = null;
  if (account) client.setActiveAccount(account);
  return { cloud: true, user: publicUser(account), error };
}

async function getToken() {
  if (!runtime?.cloud) return null;
  if (!client || !account) throw new Error("Sign in to continue.");
  try {
    // Cached token, else the refresh token. MSAL's hidden-iframe fallback is skipped: the page refuses to be framed
    // (frame-ancestors 'none'), so it could only time out. An expired refresh token asks for interaction instead.
    const request = { scopes, account, cacheLookupPolicy: CacheLookupPolicy.AccessTokenAndRefreshToken };
    return (await client.acquireTokenSilent(request)).accessToken;
  } catch (error) {
    if (error instanceof InteractionRequiredAuthError) {
      await client.acquireTokenRedirect({ scopes, account });
      throw new Error("Signing in again…");
    }
    throw new Error("Sign-in expired or could not be verified. Sign in again.");
  }
}

async function signIn() {
  if (!client) throw new Error("Cloud sign-in is not enabled for this workspace.");
  await client.loginRedirect({ scopes, prompt: "select_account" });
}

async function signOutUser() {
  const signedIn = account;
  account = null;
  if (client) await client.logoutRedirect({ account: signedIn || undefined });
}

window.InvoiceStudioAuth = {
  bootstrap,
  getToken,
  signIn,
  signOut: signOutUser,
  currentUser: () => publicUser(account),
};
