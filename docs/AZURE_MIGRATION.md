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

Google Cloud continues from `Inv_studio`. Carry future shared fixes into this repository through reviewed PRs that preserve the Azure sign-in, AI and deployment changes. Keep the GitHub access token outside the repository and container build context.
