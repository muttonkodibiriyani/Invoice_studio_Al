# Invoice Studio runbook

Live app: https://inv-studio-740495548022.web.app. GCP project `productintelligence-beeb3` is **shared with Product
Intelligence** (`pi-api`, `pi-capture`, `pi-sephora`, the default Hosting site, and the Firebase Authentication user
pool): never touch those, and never change project-wide Auth settings without the owner's OK.

| Piece | Where |
|---|---|
| Backend | Cloud Run `inv-studio-api`, `us-central1`. Image repo `us-central1-docker.pkg.dev/productintelligence-beeb3/inv-studio/app`. Runtime identity `inv-studio-runtime@…`. 4 CPU / 8 GiB, concurrency 20, timeout 300 s, min 0 / max 1 instance per revision, instance-based billing (cpu-throttling off), startup CPU boost, gen2, TCP startup probe (240 s). |
| Frontend | Firebase Hosting site `inv-studio-740495548022`; config `hosting/firebase.json` (Firebase CLI form). `/api/**`, `/health` and `/auth/**` are rewritten to the service; `/set-password` serves the static `set-password.html`; everything else serves `index.html`. Released with `scripts/hosting_release.py` (section 1). |
| Database | Cloud SQL Postgres 16 `inv-studio-db` (`db-custom-1-3840`, zonal, `us-central1`). Deletion protection on, daily backups (03:00 UTC, 14 retained), point-in-time recovery with 7 days of logs (enabled 20 Oct 2026 09:07 UTC; no earlier restore point exists), storage auto-resize to 50 GB, project lien against deletion. |
| Files | Bucket `productintelligence-beeb3-inv-studio-evidence` (uploads/exports; versioning + 7-day soft delete). Source bundles: `productintelligence-beeb3-inv-studio-source/git-bundles/` (private; public access prevention enforced). |
| Secrets | Secret Manager `inv-studio-database-url`, `inv-studio-vault-key` (mounted as env from `latest`). Never print them; never paste them into tm8. |
| Source | Git repo `~/inv-studio-golive/repo/inv-studio` on the ops host. Branch `main` is the live line: since 23 Oct 2026 the same commit as `go-live/auth` (users and sign-in, with the scanner line `go-live/ocr` merged in); the live image's `org.opencontainers.image.revision` label names the exact commit that runs. Tag `recovered-live-2026-10-08` is the code recovered from the 8 Oct live image. `live/ocr` (that code plus the scanning engine) was live on 23 Oct 2026 from 07:12 to 21:13 UTC only: never build from it again (section 1). Off-host copy: the bundle in the source bucket. |
| Monitoring | Uptime check `inv-studio /health` (60 s, 3 regions). Alert policies "Invoice Studio: …" (uptime failing, 5xx ratio > 5 %, container crash / startup probe failure, Cloud SQL disk > 80 %) → email channel "Invoice Studio alerts (Tharak)". The uptime check keeps one instance running around the clock; with instance-based billing that is the largest fixed cost (about US$7–8 a day). |

Operator environment on the ops host: `. ~/inv-studio-golive/tools/env.sh` (private gcloud config, project set, prompts
off). Releases need no Firebase CLI or key file: `scripts/hosting_release.py` uses the gcloud access token. Both will move
to the least-privilege deployer identity once that plan is agreed (see the deploy-identity plan).

## 1. Deploy a new version

Rule: **a build never goes live by itself.** Traffic is pinned to a named revision; a candidate is deployed with no
traffic and tested by the owner on a Hosting preview channel; then a fresh revision with the live settings is promoted.

**Live now (since 23 Oct 2026 21:13 UTC): the users and sign-in release.** Revision `inv-studio-api-users-live` runs
image tag `users-3633d1c` (`app@sha256:15a9b53b124d…`, the linux/amd64 manifest in the pushed index `e4acfe7593f4…`):
`go-live/auth` `3633d1c`'s `app/` (tree `b7d4f0ec…`) on the 8 Oct live image, with the live settings. Hosting version
`8e5145b49e9a2183` holds the static files of the same export (`~/inv-studio-golive/build/3633d1c`). The release keeps
everything that was live before it: the scanning engine (07:12 UTC) and its unit-of-measure and AI-spend changes
(`inv-studio-api-cost-live`, 08:45 UTC). The owner tried the no-traffic candidate `inv-studio-api-users-c3` (same
digest) on the `ocr-candidate` preview and approved at 21:07 UTC; traffic and Hosting moved at 21:13 UTC. The `ocr`
tag stays on `users-c3` until that preview expires on 29 Oct; then remove it with
`gcloud run services update-traffic inv-studio-api --region us-central1 --remove-tags ocr`.

Build every later change, scanner fixes included, from `main` (= `go-live/auth`; merge `go-live/ocr` in first if the
scanner line has moved), never from `live/ocr` or an older candidate: such a build would take the users release back
off. Before promoting a build, check that it contains what runs: `git merge-base --is-ancestor 3633d1c <commit>` must
succeed (`3633d1c` is the live image's `org.opencontainers.image.revision` label; use the label of whatever runs then).
What was checked for the scanner is in `docs/PRODUCTION_READINESS.md`.

1. **Build** from a clean export of the commit you mean to ship, never from the working tree. Run the tests first:
   `~/inv-studio-golive/venv/bin/python -m pytest tests -q`.
   ```bash
   cd ~/inv-studio-golive/repo/inv-studio      # shared with the scanner work: never switch branches, stash or clean it
   SHA=$(git rev-parse --short main); SRC=~/inv-studio-golive/build/$SHA   # main is the live line; never live/ocr
   git merge-base --is-ancestor 3633d1c "$SHA" && mkdir -p "$SRC" && git archive "$SHA" | tar -x -C "$SRC"
   IMG=us-central1-docker.pkg.dev/productintelligence-beeb3/inv-studio/app
   export DOCKER_CONFIG=$(mktemp -d)                                       # private login; never touch ~/.docker
   gcloud auth print-access-token | docker login -u oauth2accesstoken --password-stdin https://us-central1-docker.pkg.dev
   ```
   - **App-only change** (the usual case: `git diff --stat <commit of the running image>..$SHA -- samples
     scripts/warm_ocr.py pyproject.toml constraints.txt Dockerfile README.md` is empty): put the commit's `app/` on top
     of the base the live image was built from (today the 8 Oct image). Dependencies and OCR models stay byte-identical
     to production and nothing is downloaded.
     ```bash
     BASE=$IMG@sha256:<amd64 manifest digest of that base>   # 8 Oct image fdff1c4b88e5…, under the live scanner build
     docker pull "$BASE"
     cd "$SRC" && docker build -f Dockerfile.app-layer --build-arg BASE="$BASE" \
       --label org.opencontainers.image.revision=$(git -C ~/inv-studio-golive/repo/inv-studio rev-parse "$SHA") -t "$IMG:users-$SHA" .
     ```
     Each app-layer build adds two layers; once the image has grown by about ten, do a full rebuild instead.
   - **Dependency or Dockerfile change**: full rebuild, `cd "$SRC" && docker build --platform linux/amd64 -t "$IMG:users-$SHA" .`
     (pins every package with `constraints.txt`; the OCR warm-up runs inside). On 22 Oct 2026 PyPI downloads inside
     BuildKit on the ops host ran at about 30 kB/s, so allow hours or build elsewhere.

   Check that the image's `app/` is exactly the commit's, then push and note the digest:
   ```bash
   docker run --rm --network none --entrypoint sh "$IMG:users-$SHA" -c 'cd /opt/inv-studio && find app -type f -exec sha256sum {} + | sort -k2' > /tmp/in-image.txt
   (cd "$SRC" && find app -type f -exec sha256sum {} + | sort -k2) | diff - /tmp/in-image.txt && echo identical
   docker push "$IMG:users-$SHA"
   docker buildx imagetools inspect "$IMG:users-$SHA"   # deploy by digest, not by tag: the linux/amd64 manifest's,
                                                        # not the index's (every revision runs an amd64 manifest)
   rm -rf "$DOCKER_CONFIG"; unset DOCKER_CONFIG
   ```
2. **Pin traffic** to the revision that is live now (skip if already pinned; `--no-traffic` below also does it):
   ```bash
   LIVE=$(gcloud run services describe inv-studio-api --region us-central1 --format='value(status.traffic[0].revisionName)')
   gcloud run services update-traffic inv-studio-api --region us-central1 --to-revisions "$LIVE=100"
   ```
3. **Preview channel** (the candidate needs its address). Never use `firebase hosting:channel:deploy` without
   `--no-authorized-domains`: by default it adds the channel to the project's Auth authorized domains, which are shared
   with Product Intelligence. The script never calls the Auth API:
   ```bash
   cd "$SRC" && python3 -I scripts/hosting_release.py channel users-candidate --ttl-days 7   # prints the URL and expiry
   ```
   Sign-in is email and password only, which works on any address. If Google sign-in is ever enabled, a preview address
   would need to be an authorized domain (a project-wide change: ask the owner).
4. **Candidate revision** with no traffic and two candidate-only settings:
   ```bash
   LIVE_ORIGINS=https://inv-studio-740495548022.web.app,https://inv-studio-740495548022.firebaseapp.com
   gcloud run deploy inv-studio-api --region us-central1 --image "$IMG@sha256:<digest>" --no-traffic --tag candidate \
     --revision-suffix <name> \
     --update-env-vars "^@^INV_STUDIO_RECOVER_JOBS=0@INV_STUDIO_ALLOWED_ORIGINS=$LIVE_ORIGINS,<preview channel URL>"
   ```
   - `INV_STUDIO_RECOVER_JOBS=0`: a running app takes over unfinished jobs whose holder has stopped renewing its
     heartbeat for 120 s, and queues them again. A candidate starting next to the live instance must take over
     nothing, not even from a live instance that stalls (`docs/AZURE.md` §9).
   - The preview URL in `INV_STUDIO_ALLOWED_ORIGINS`: the API refuses writes from origins it does not know, and
     create-password / reset links point at the address the administrator is using.
   - These settings stay in the service's template until step 7 resets them. Until then, any other `gcloud run deploy`
     or `services update` would carry them into its revision: reset them the same way.
   The candidate shares the production database, bucket and sign-in pool. The users release only adds the
   `app_users` table, which older revisions ignore. Redeploying a fixed candidate with `--tag candidate` moves the tag.
5. **Release the static files to the preview**, with the API sent to the candidate tag:
   ```bash
   cd "$SRC" && python3 -I scripts/hosting_release.py release users-candidate --tag candidate --message "$SHA"
   ```
   Smoke test: `/`, `/set-password` and `/health` answer 200 on the preview; `/api/session` without a token is 401; the
   headers include `content-security-policy`. Then the owner tests on the preview URL: sign in with the email and
   password of the live site (the owner's sign-in is shared, so the app never resets it; see section 4); Users → add a
   test user → open the link → set a password → sign in as that user; as that user "Forgot password?" (no email is sent)
   → the row shows "Reset requested" → reset link → new password; disable / enable; delete; upload, review and export one
   invoice. Test users are real accounts in the shared pool: delete them in the app afterwards. Do not leave an upload
   processing on the preview when the live instance may restart (the live revision's own startup recovery would mark it
   failed).
   A change to the static files only (`app/static`, `hosting/public`) needs no new image: export the commit and release
   it to the preview with the same candidate. The image's own copy of those files is served only on the bare Cloud Run
   address, never through Hosting; go-live then deploys the tested digest and releases Hosting from the newer export.
6. **Owner approval.** Nothing below runs before it.
7. **Go live**: a fresh revision from the same digest with the live settings (not the candidate revision, which has
   `INV_STUDIO_RECOVER_JOBS=0` and the preview origin), checked on its own tag, then traffic, then the static files:
   ```bash
   gcloud run deploy inv-studio-api --region us-central1 --image "$IMG@sha256:<digest>" --no-traffic --tag golive \
     --revision-suffix <name>-live --remove-env-vars INV_STUDIO_RECOVER_JOBS \
     --update-env-vars "^@^INV_STUDIO_ALLOWED_ORIGINS=$LIVE_ORIGINS"
   curl -s https://golive---inv-studio-api-33zcb55laa-uc.a.run.app/health
   gcloud run services update-traffic inv-studio-api --region us-central1 --to-revisions inv-studio-api-<name>-live=100
   cd "$SRC" && python3 -I scripts/hosting_release.py release live --live --message "$SHA"
   gcloud run services update-traffic inv-studio-api --region us-central1 --remove-tags candidate,golive
   ```
   (`cd "$SRC/hosting" && firebase deploy --only hosting --project productintelligence-beeb3` is equivalent for the live
   channel; `firebase.json` names the site.)
8. **Verify**: `curl -s https://inv-studio-740495548022.web.app/health`, sign in, open Users, upload one invoice. Watch
   Logging for `severity>=ERROR` and the "Invoice Studio" alert policies for 30 minutes. Record the revision and Hosting
   version ids on the task. The preview channel expires by itself; revisions are kept (never delete them).

## 2. Roll back

Backend (seconds, no rebuild):
```bash
gcloud run revisions list --service inv-studio-api --region us-central1 --format='table(name,status.conditions[0].status,metadata.creationTimestamp)' | head
gcloud run services update-traffic inv-studio-api --region us-central1 --to-revisions <previous-revision>=100
```
Frontend: re-release the previous Hosting version (Console → Hosting → Release history, or):
```bash
TOKEN=$(gcloud auth print-access-token)
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "https://firebasehosting.googleapis.com/v1beta1/sites/inv-studio-740495548022/channels/live/releases?versionName=sites/inv-studio-740495548022/versions/<versionId>"
```
Live pair: revision `inv-studio-api-users-live` + Hosting version `8e5145b49e9a2183` (the users and sign-in release,
23 Oct 2026 21:13 UTC). Roll it back to the pair before it: revision `inv-studio-api-cost-live` + Hosting version
`8e2243be6414c5f4` (the scanner with its unit-of-measure and AI-spend changes, which changed no static files):
```bash
gcloud run services update-traffic inv-studio-api --region us-central1 --to-revisions inv-studio-api-cost-live=100
# then re-release Hosting version 8e2243be6414c5f4 with the curl above
```
Not to `inv-studio-api-scan-live` (the scanner without those changes) or `inv-studio-api-00099-7n2` (8 Oct, before the
scanner; both with the same Hosting version), or live changes go too. Database: no step; the users release only adds the
`app_users` table, which older revisions ignore (the AI-spend change records into the existing audit table). After a
rollback to that pair only the addresses in `INV_STUDIO_ALLOWED_EMAILS` can sign in (the old code has no user
directory); invited users keep their rows and get access back when the release is restored (traffic back to
`inv-studio-api-users-live`, Hosting back to `8e5145b49e9a2183`).

## 3. Restore the database

Take an on-demand backup before any restore: `gcloud sql backups create --instance inv-studio-db --description "pre-restore"`.

Preferred, non-destructive: clone to a point in time, inspect, then decide.
```bash
gcloud sql backups list --instance inv-studio-db
gcloud sql instances clone inv-studio-db inv-studio-db-restore --point-in-time "2026-10-21T10:00:00Z"   # last 7 days, not before 20 Oct 09:07 UTC
```
Point the app at the clone only if needed: add a new version of `inv-studio-database-url` whose instance connection name
is the clone's, then `gcloud run services update inv-studio-api --region us-central1 --add-cloudsql-instances productintelligence-beeb3:us-central1:inv-studio-db-restore`.
That creates a new revision, which reads the new secret version when it starts; traffic is pinned, so move it with
`update-traffic --to-revisions <new revision>=100`. The clone is a second billed instance: remove it when done, with the
owner's OK.

In place (overwrites everything after the backup; the app errors for the duration):
```bash
gcloud sql backups restore <BACKUP_ID> --restore-instance inv-studio-db
```
Invoice files: bucket objects are versioned with 7-day soft delete; restore with `gcloud storage restore` / object versions.

## 4. Users and sign-in

Normal path (administrator, in the app): **Users** in the sidebar. The app never sends email: the administrator copies
each link and sends it to the person.
- Add: email, name, role → "Create user and get link". The link is shown once, works once and expires; the code travels
  after `#` in the address, so it never reaches a server log. The person sets a password on the Invoice Studio page and
  signs in; their status moves from Invited to Active.
- Reset: the person clicks "Forgot password?" on the sign-in screen → their row shows "Reset requested" → the
  administrator clicks "Reset-password link" and sends it. "Forgot password?" sends no email and says so; it answers the
  same whether or not the address exists, records at most one request per account per 15 minutes (only for people who
  already have a row: the owner's row appears at their first sign-in), and never contacts the sign-in service. Its
  per-address limit (30 per 15 minutes) is coarse: behind Hosting the service sees Google edge addresses, not the
  person's.
- Shared sign-ins: a row whose sign-in Invoice Studio did not create (the owner's, which Product Intelligence also uses)
  shows "Shared sign-in" instead of a link button, and Delete only removes the row. Its password is changed where the
  sign-in was created, for example Firebase Console → Authentication → Users → the row's menu → Reset password, which
  sends Firebase's standard email (from `noreply@productintelligence-beeb3.firebaseapp.com`; it can land in spam). The
  new password then applies everywhere that sign-in is used. The live version before the users release had a "Send password reset email" button that sent that
  same email; the users release removes it (decision: the administrator copies the link).
- Remove access: "Disable" (immediate) or "Delete" (their invoices stay). You cannot disable, demote or delete yourself
  or the last active administrator; promote someone first.
- Roles: Member = upload, review, export, and teach from exported invoices. Administrator = everything, including
  deleting invoices, approving or rejecting fine-rule feedback (the signed-in administrator is recorded as approver),
  Settings, Engines & AI, policy / templates / fine-rules configuration and Users. Teaching stays open to members in
  v1 by decision; restricting it is an owner question.
- Adding users, reset links and disabling an app-created sign-in need the runtime identity to manage users in the
  Firebase project. Since 22 Oct 2026 18:59 UTC it holds the custom role `invStudioUserAdmin` (deploy-identity plan,
  step 3): `firebaseauth.users.create` (Add user), `.sendEmail` (password links), `.update` (Disable / Enable;
  disabling also revokes sessions), `.delete` (Delete) and `.get` (lookups), and no Auth settings. If the Users page still answers
  "Invoice Studio is not yet allowed to create or change sign-ins" (the API's own message is "The identity provider
  rejected the request: PermissionDeniedError"), nothing was changed: add only the permission for the refused action
  to that role, never `roles/firebaseauth.admin`.

Which sign-ins the app manages. The Firebase user pool is shared with Product Intelligence, so every row carries
`login_origin`:
- `app`: the app created this sign-in through an invitation. Disable also blocks it at the sign-in service and revokes
  its sessions; Delete removes it; reset links can be issued.
- `existing`: a sign-in the app did not create (the bootstrap owner, or any login that already existed in the pool).
  Disable and Delete only remove access to Invoice Studio; the sign-in itself is never changed, and no reset link is
  issued for it (its owner resets it where it was created).
- An address that already has a sign-in in the pool (for example a Product Intelligence login) cannot be invited: the
  app refuses rather than take over another app's account. Use a different address, or an operator adds the existing
  sign-in as `existing` by SQL (below) with the owner's OK.

Bootstrap: addresses in the service's `INV_STUDIO_ALLOWED_EMAILS` become administrators (`existing`) on their first
sign-in. Keep it to the owner only. While an address is listed there the app refuses to delete it.

Break-glass, in Cloud SQL Studio (the connection string, including its password, is in the `inv-studio-database-url`
secret; read it only in a private terminal). Every user-management action is in the `audit` table (`user.*` events)
without links or codes.
```sql
-- Block someone at once (also disable an `app` sign-in in the Firebase Console -> Authentication -> Users):
UPDATE app_users SET status='disabled' WHERE email='<email>' AND deleted_at IS NULL;
-- No active administrator left: restore one who has signed in before.
UPDATE app_users SET role='admin', status='active' WHERE email='<email>' AND deleted_at IS NULL;
-- A bootstrap owner was deleted by SQL: bring the row back (their next sign-in is refused until then).
UPDATE app_users SET deleted_at=NULL WHERE email='<email>' AND deleted_at IS NOT NULL;
-- Give an existing pool sign-in access (uid from the Firebase Console), as a member:
INSERT INTO app_users (uid,email,name,role,status,org_id,invited_by,invited_at,login_origin)
VALUES ('<uid>','<email>','','member','active','default','operator',
        to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"+00:00"'),'existing');
```

## 5. Never

Push the repo anywhere public. Delete revisions, images, backups, secrets, buckets, users or data without the owner's OK.
Change min-instances, CPU allocation or HA (cost) without the owner's OK. Touch `pi-*` services, the default Hosting site,
or project-wide Auth settings (email templates, password policy, MFA, authorized domains, multi-tenancy). Grant the
runtime identity a role that can change those settings (`roles/firebaseauth.admin` includes `firebaseauth.configs.update`;
see the deploy-identity plan). Print secrets, tokens, keys, oobCodes or password links into logs, chats or tm8.
