import { getApp, getApps, initializeApp } from "@firebase/app";
import {
  GoogleAuthProvider,
  confirmPasswordReset,
  getAuth,
  onAuthStateChanged,
  sendEmailVerification,
  reload,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut,
  verifyPasswordResetCode,
} from "@firebase/auth";

let runtime = null;
let auth = null;
let currentUser = null;
let initialUserPromise = null;

function requireFirebaseConfig(config) {
  const firebase = config?.firebase;
  const required = ["apiKey", "authDomain", "projectId", "appId"];
  if (!firebase || required.some((field) => !firebase[field])) {
    throw new Error("Cloud sign-in is not configured completely. Contact the workspace administrator.");
  }
  return firebase;
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

function waitForInitialUser() {
  if (!initialUserPromise) {
    initialUserPromise = new Promise((resolve, reject) => {
      const unsubscribe = onAuthStateChanged(auth, (user) => {
        currentUser = user;
        unsubscribe();
        resolve(user);
      }, (error) => {
        unsubscribe();
        reject(new Error(error?.code === "auth/network-request-failed" ? "Firebase sign-in could not reach the network." : "Firebase sign-in could not be initialized."));
      });
    });
  }
  return initialUserPromise;
}

async function bootstrap() {
  runtime = await readPublicConfig();
  if (!runtime.cloud) return { cloud: false, providers: [], user: null };
  const firebase = requireFirebaseConfig(runtime);
  const firebaseApp = getApps().length ? getApp() : initializeApp(firebase);
  auth = getAuth(firebaseApp);
  if (typeof firebase.tenantId === "string" && firebase.tenantId) auth.tenantId = firebase.tenantId;
  const user = await waitForInitialUser();
  return { cloud: true, providers: Array.isArray(firebase.providers) ? firebase.providers : [], user };
}

async function getToken(forceRefresh = false) {
  if (!runtime?.cloud) return null;
  if (!currentUser) throw new Error("Sign in to continue.");
  return currentUser.getIdToken(forceRefresh);
}

async function signInGoogle() {
  if (!auth || !runtime?.firebase?.providers?.includes("google")) throw new Error("Google sign-in is not enabled for this workspace.");
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  const result = await signInWithPopup(auth, provider);
  currentUser = result.user;
  return currentUser;
}

async function signInPassword(email, password) {
  if (!auth || !runtime?.firebase?.providers?.includes("password")) throw new Error("Email sign-in is not enabled for this workspace.");
  const result = await signInWithEmailAndPassword(auth, email, password);
  currentUser = result.user;
  return currentUser;
}

// Password creation and reset happen on the app's own /set-password page with a code the administrator hands over.
// The app never asks Firebase to send email.
async function ensureAuth() {
  if (!auth) await bootstrap();
  if (!auth) throw new Error("Cloud sign-in is not enabled for this workspace.");
  return auth;
}

async function verifyResetCode(code) {
  return verifyPasswordResetCode(await ensureAuth(), code);
}

async function confirmReset(code, password) {
  await confirmPasswordReset(await ensureAuth(), code, password);
}

async function sendVerification() {
  if (!currentUser) throw new Error("Sign in first.");
  await sendEmailVerification(currentUser);
}

async function refreshIdentity() {
  if (!currentUser) throw new Error("Sign in first.");
  await reload(currentUser);
  await currentUser.getIdToken(true);
  return currentUser;
}

async function signOutUser() {
  if (auth) await signOut(auth);
  currentUser = null;
}

window.InvoiceStudioAuth = {
  bootstrap,
  getToken,
  signInGoogle,
  signInPassword,
  verifyResetCode,
  confirmReset,
  sendVerification,
  refreshIdentity,
  signOut: signOutUser,
  currentUser: () => currentUser,
};
