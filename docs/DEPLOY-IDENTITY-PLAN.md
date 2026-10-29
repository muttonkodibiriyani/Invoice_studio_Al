# Deploy identity and secret hygiene: proposal (nothing here is applied without the owner's OK)

## Today (re-checked 22 Oct 2026)

- `firebase-adminsdk-fbsvc@productintelligence-beeb3.iam.gserviceaccount.com` holds **roles/owner** (plus its normal
  `firebase.sdkAdminServiceAgent`) and has **three user-managed keys**, none disabled:
  - `614d56c349…` (30 Sep): on the ops host. Every Invoice Studio operation of this go-live work used it, and Product
    Intelligence also deploys with it, so it cannot be retired before PI has its own deploy identity.
  - `eb4ae8a035…` (4 Oct): created by the project owner in the Console. Used from the ops host for Invoice Studio deploys,
    last on 8 Oct 14:58 UTC (the deploy that is live today). No copy of it was found on the ops host.
  - `d32fb89601…` (20 Oct 09:32 UTC): created by the project owner in the Console.
- Keys created from the Console by the project owner on 20 Oct 2026 09:31–09:32 UTC on other accounts:
  `vertex-express@` (`7476d70ad8…`), and the Compute and App Engine default accounts (`c58186de10…`, `3ff0088922…`),
  which carry **roles/editor**. A leaked Editor key is nearly as bad as the Owner key.
- Copies of these new key files (including the Owner key) were uploaded into a different tm8 Space (`019fbd5a…`) on
  20 Oct between 09:36 and 09:41 UTC. Only key ids were read, never the contents. Anyone who can open those files has
  Owner-level control of the project, Product Intelligence included.
- On 20 Oct at about 18:25 UTC "muji-wa" was set up from the ops host with the Owner key `d32fb89601`: four service
  accounts (`muji-wa-agent`, `-deployer`, `-importer`, `-webhook`) and new grants. `muji-wa-deployer@` holds about 25 admin
  roles, including **Project IAM Admin** (it can grant itself anything, so in effect a second Owner), Secret Manager Admin,
  Storage Admin, Cloud Run Admin, Service Account Admin and Monitoring Editor, and has a permanent key (`b86bc1c2d1…`) in a
  file on the ops host. It can read Invoice Studio's database password and invoice files. Its admin audit log (from its
  creation to 22 Oct) shows changes only to its own resources (functions, Firestore database, secrets, network and log
  bucket in me-central1), grants to its own accounts and the Compute default account, and an exclusion on the project's
  `_Default` log sink for its own services; nothing of Invoice Studio's. Reads of secrets and files are not logged by default.
- The Cloud Run runtime identity `inv-studio-runtime@` is least-privilege (`cloudsql.client`, `aiplatform.user`,
  `firebaseauth.viewer`, and since 22 Oct 18:59 UTC the custom role `invStudioUserAdmin` from step 3, which lets the
  users release create users, issue password links, disable and delete users) and has no keys. `inv-studio-build@`
  exists already with `artifactregistry.writer` + `logging.logWriter` and no keys.
- Secrets: `inv-studio-database-url` (contains the DB password) and `inv-studio-vault-key` (Fernet key encrypting stored
  API credentials), both from 4 Oct 2026, never rotated. The Firebase web API key the app serves has no HTTP-referrer
  restriction (the other browser key, restricted to the PI domains, is Product Intelligence's).
- The sign-in pool is shared with Product Intelligence. Multi-tenancy is off for the project, so an Invoice Studio tenant
  (decision D2) needs a project-wide Identity Platform change (allow tenants) first; until then the app works in the
  shared pool and only changes sign-ins it created.

## Proposed steps (each one waits for an explicit OK)

0. **Contain the keys** (urgent, independent of Invoice Studio): delete the Console key `d32fb89601` on the Owner account
   and the two Editor keys `c58186de10` and `3ff0088922`; disable `eb4ae8a035` now and delete it a week later; keep
   `vertex-express`'s key only if something uses it; delete the key files in the other tm8 Space. Decide what muji-wa is:
   it should get its own project, or at least lose Project IAM Admin, Secret Manager Admin and Storage Admin here, and
   use keyless deploys. Keep `614d56c349` until step 4.
1. **Create `inv-studio-deployer@`** (no key at creation). Grants, all resource-scoped where the API allows it:
   `roles/run.admin` on service `inv-studio-api` only; `roles/iam.serviceAccountUser` on `inv-studio-runtime@` only;
   `roles/artifactregistry.writer` on repository `inv-studio` only; project-level `roles/firebasehosting.admin` (the only
   way to release Hosting; it also covers the PI site, so this is the one broad grant), `roles/logging.viewer`,
   `roles/monitoring.viewer`, `roles/cloudsql.viewer`. It cannot read secrets, change IAM, or touch `pi-*`.
2. **How it is used.** Preferred: you (Owner) impersonate it from your own machine
   (`gcloud … --impersonate-service-account=inv-studio-deployer@…`, needs `roles/iam.serviceAccountTokenCreator` on the SA
   for your user). For unattended deploys from the ops host: one key, file mode 0600 in a private directory, rotated every
   90 days, listed in the runbook. Decide which.
3. **Let the runtime identity manage users** (**done** 22 Oct 2026, see below), with a custom role rather than
   `roles/firebaseauth.admin`. The predefined role also holds `firebaseauth.configs.update`, `configs.getHashConfig` and
   `configs.getSecret`, i.e. it could change the project-wide sign-in settings Product Intelligence uses and read the
   password-hash key. The app needs only:
   ```bash
   gcloud projects get-iam-policy productintelligence-beeb3 --format=json > ~/inv-studio-golive/snapshot/iam-before-user-admin.json
   gcloud iam roles create invStudioUserAdmin --project productintelligence-beeb3 --title "Invoice Studio user admin" \
     --description "Create, read, update and delete Firebase Auth users and issue their password links. No Auth settings." \
     --permissions firebaseauth.users.create,firebaseauth.users.get,firebaseauth.users.update,firebaseauth.users.delete,firebaseauth.users.sendEmail --stage GA
   gcloud projects add-iam-policy-binding productintelligence-beeb3 --condition None \
     --member serviceAccount:inv-studio-runtime@productintelligence-beeb3.iam.gserviceaccount.com \
     --role projects/productintelligence-beeb3/roles/invStudioUserAdmin
   gcloud projects get-iam-policy productintelligence-beeb3 --format=json > ~/inv-studio-golive/snapshot/iam-after-user-admin.json
   ```
   Then on the candidate: add a test user, open the link, set a password, sign in, disable, delete. A permission error
   shows as "The identity provider rejected the request: PermissionDeniedError"; add only the missing
   `firebaseauth.users.*` permission. Limitation: in a shared pool any Auth role is project-wide, so this identity could
   change any user in the pool, PI's included; the app's own checks stop that, IAM does not. An Invoice Studio tenant
   (D2) with a tenant-scoped grant would remove it.

   **Done** on the project owner's instruction (22 Oct 2026, relayed by the coordinator), exactly as above, from the ops
   host as `firebase-adminsdk-fbsvc@`: role created 18:59:42 UTC, binding added 18:59:46 UTC. The IAM policy diff
   between the two snapshots is that one binding; no deny policies apply. **Check (19:13 UTC):** every call the app
   makes needs one of the five permissions (`create_user` → `users.create`; password link → `users.sendEmail`;
   disable, enable and session revocation → `users.update`; delete → `users.delete`; lookups and the ID-token revocation
   check → `users.get`), so none is missing. The ops identity cannot act as `inv-studio-runtime@`
   (`iam.serviceAccounts.getAccessToken` is not granted, deliberately), so no call has yet run as the runtime identity.
   A throwaway `@example.com` sign-in went through the rest of the flow with the candidate's own code, as the ops
   identity: create, link for the preview, link verified (right email), password set, link refused on reuse, sign-in,
   disable (sign-in then refused: `USER_DISABLED`), delete, gone from the pool. The first call as the runtime identity
   is the owner's test on the preview (runbook §1.5); a refusal there shows as above and needs only the permission for
   that action.
4. **Remove `roles/owner` from `firebase-adminsdk-fbsvc@`** (keep `firebase.sdkAdminServiceAgent`) and **delete its keys**,
   once Product Intelligence deploys with its own identity and the ops host has switched to step 2.
5. **Rotate the DB password**: add a new version of `inv-studio-database-url` with the new password, then
   `gcloud sql users set-password … --prompt-for-password`, then deploy a new revision (it reads `latest` when it starts)
   and move traffic to it (traffic is pinned). Only new database connections the old revision opens in between fail,
   about a minute's worth.
6. **Rotate the vault key**: the store uses a single Fernet key, so rotation needs a small code change (MultiFernet with
   old+new keys, a one-off re-encrypt of the `secrets` table, then drop the old key). Scheduled after go-live, on the branch,
   with tests.
7. **Restrict the Firebase web API key** the app serves to the two Hosting origins (preview channels would then need
   their address added while in use). Also restrict it to the Identity Toolkit / Secure Token APIs.
8. Keep the on-demand backup and the pre-change snapshots (`~/inv-studio-golive/snapshot/`) until all of the above is done.
