# Azure repository baseline

This repository is the complete starting copy for the Azure build of Invoice Studio. It contains the app, browser UI, tests, synthetic samples, Dockerfiles, Azure Bicep, deployment workflow and runbooks. The import source is the local `Inv_studio` checkout at `331ae390523e8c577c6a4a36090d789b9a4d4586`. Access to the upstream repository was unavailable during this import, so this is not a claim that the source is its latest remote main.

The existing repository `LICENSE` is preserved. The imported source's MIT copyright and permission notice is retained in `LICENSE.source`.

This import does not complete the enterprise architecture changes. Do not deploy this baseline as the organization's approved Azure application: Firebase authentication and a direct Foundry client are still present. The Azure deploy workflow is manual and this import does not run it.

## Implementation order

The live work plan is the `invoice_studio` craft, blueprint `01a18177-9fd4-75ff-bba0-08d61bcb535e`, with target drawing `01a186cf-b7e2-7daa-859e-ae74cc792d32`. Continue that plan against this repository; do not import the application a second time.

1. Verify this import and its CI, then implement workforce Entra ID sign-in in the backend and UI. Remove Firebase, password and invite flows from the Azure build. Bind admin and member app roles to Entra groups.
2. Prepare the access request, parameter template, deployment preflight and Cloud Shell runbook. Check the complete app locally with Entra test tokens, PostgreSQL and a Blob emulator, including read, review and Excel export.
3. Implement the reviewed target: assigned landing zone and IPAM ranges, internal ingress, private endpoints and DNS, hub firewall routing, and Citadel as the only AI route. Keep AI off until Citadel is configured; remove direct Foundry credentials and calls.
4. Add the organization's required SAST, dependency, image, secret and IaC scans, approved base image and required PR checks. Review each change before merging.
5. Record the tested commit, image digest, parameter checks, test results, accepted POC limits and rollback steps. Tarkesh performs the cloud deployments from the runbook after access and the release gates are satisfied.

## Values the platform team must supply

| Area | Required values or decisions |
|---|---|
| Entra | Workforce tenant, app registrations, audience, redirect URI, admin/member groups, app roles, Conditional Access requirements |
| Azure access | Subscription, resource group, region, operator/deploy roles including role-assignment permission, providers and quota |
| Networking | Landing zone, IPAM subnets, hub/firewall details, approved egress destinations, central private DNS zones, central registry or ACR Premium |
| Citadel | Endpoint, API contract, authentication/audience, approved vision models, request limits, retention and audit policy |
| Delivery | Required scanners and pipeline, approved image, private-network runner |
| Release | Availability tier, RTO/RPO, POC data policy and exit criteria, accepted temporary exceptions |

Missing values remain explicit placeholders. A local rehearsal cannot establish tenant access, private routing, real group membership or Citadel availability; verify those in the target environment before release.

## Validation boundary

The source passed 662 unit/integration tests and Ruff in the preceding migration preparation. Its package and Bicep compiled, and Docker build stages exercised all three readers. A full container startup check was blocked by insufficient shared disk space and remains open. The migration PR's checks validate the imported repository separately.

The source's scan-reader tests require OCR packages and cached models and are excluded from the default pytest run. Successful default CI does not establish that those tests or an Azure end-to-end deployment passed.

Google Cloud continues from `Inv_studio`. Shared fixes and releases come in through the sync below, never by deploying Azure from `Inv_studio`. Keep the GitHub access token outside the repository and container build context.

## Bringing an Inv_studio release in

Each verified Inv_studio release tag, and any shared fix merged there between releases, comes into this repository the same way. The two repositories have unrelated histories. A release therefore arrives as the upstream diff since the last synced commit, which `.inv-studio-sync` records (`5f7a26b`, the release-1 content, carried by PR #3).

"Azure changes" means every file in which this repository's `main` differs from that commit. They include the import files, the Entra sign-in (the backend token check, `app/static/entra-auth*`, the removal of Firebase, invites and password links), the Citadel AI settings, the Azure Bicep and parameters, the deploy workflow and `scripts/azure_deploy.py`, the docs, and this script. The script works out this list each time, so a later Azure PR never has to be registered anywhere.

1. Branch from current `origin/main`: `git fetch origin && git checkout -b sync/inv-studio-<tag> origin/main`.
2. Preview with `scripts/sync_inv_studio.sh --check <tag>`. It prints the upstream file count, the Azure change count and every OVERLAP, meaning a file that both upstream and Azure changed. It changes nothing.
3. Run `scripts/sync_inv_studio.sh <tag>`. It applies the upstream diff with a three-way merge, stages it and records the tag's commit in `.inv-studio-sync`. It commits and pushes nothing. It stops on a conflict, or if any file still differs from upstream without being an Azure change.
4. For each OVERLAP or conflict, keep the Azure behaviour and take the upstream fix around it. A file Azure deleted, such as the Firebase hosting files, `app/user_admin.py` or `scripts/hosting_release.py`, stays deleted. Carry an upstream fix to such a file into its Azure replacement, or state in the PR that it does not apply. Once resolved, `git add` the files. The script's final check has not run on a conflict, so confirm by hand that `git diff --name-only <tag> $(git write-tree)` lists only Azure changes and `.inv-studio-sync`.
5. Run the default checks the way CI does: `uv pip install -e '.[test,cloud,azure]'`, then `.venv/bin/ruff check .` and `.venv/bin/pytest -q`.
6. Commit and open a PR titled `Sync Inv_studio <tag> (<short commit>)`. The body names the tag, its full commit, the upstream PRs it carries, the OVERLAP list from step 2 with how each was resolved, and the script's final `ok` line.
7. Merge only after green CI and an ECC review by someone other than the author, at the PR's head commit. A sync never changes Inv_studio and deploys nothing.
