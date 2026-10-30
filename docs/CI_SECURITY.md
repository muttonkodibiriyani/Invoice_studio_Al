# CI security scans

The workflow `.github/workflows/security.yml` runs five jobs on every pull request and on every push to
`main`. Each job fails on a finding, so with branch protection (below) a finding blocks the merge. The
platform has not named its mandated tools or its approved base image yet: the tools here are well-known
open ones, and the approved image is the named placeholder `<APPROVED_BASE_IMAGE>@<APPROVED_BASE_IMAGE_DIGEST>`
in the `Dockerfile`. When the platform names them, swap the tool inside the job and keep the job name, so the
required checks stay the same.

Every action is pinned by commit SHA, every downloaded tool by version and SHA-256, and the workflow token is
`contents: read` only.

| Job (required check) | Scan | Tool | Blocks a merge when | Accepted / baseline |
|---|---|---|---|---|
| `sast` | SAST of `app`, `scripts`, `tools` | bandit 1.9.4, medium+ severity and confidence | a finding not in the baseline | `security/bandit-baseline.json` |
| `dependency-audit` | known vulnerabilities in every pinned package | pip-audit 2.10.1 on `constraints.txt` plus the Dockerfile's own pins (pip, setuptools, wheel, torch), built by `tools/security/audit_requirements.py` | any advisory not accepted | `security/accepted-advisories.txt` (security-review decision only) |
| `secret-scan` | secrets in the whole git history | gitleaks 8.30.1 (`gitleaks git`, all branches) | any match not in the baseline | `.gitleaksignore` (each line says why) |
| `iac-scan` | Bicep in `infra/azure` | `az bicep build` (Bicep 0.48.1) to ARM, then checkov 3.4.0 on the compiled template | a failed check not in the baseline | `security/checkov-baseline.json` |
| `container-scan` | base image pin, the base image itself, the Dockerfiles | `tools/security/check_base_image.py`, then trivy 0.76.1: `trivy image` on the pinned base (OS packages, HIGH/CRITICAL with a fix) and `trivy config` on both Dockerfiles (HIGH/CRITICAL) | an unpinned `FROM`, a fixable HIGH/CRITICAL in the base, a HIGH/CRITICAL Dockerfile misconfiguration | none |

Why the container scan looks at the base image and the Dockerfiles, not the built image: the image is about
5.6 GB with the OCR models, more than a hosted runner holds, and it is built inside Azure by ACR Tasks
(`deploy-azure.yml`). The Python packages in it are covered by `dependency-audit`. Scanning the built image
moves to the in-network runner (last section).

Bicep is compiled first because checkov's own Bicep parser cannot read `network.bicep`; the compiled ARM
template covers every module.

Baselines hold findings that were already there when the scan was added (29 Oct 2026); they are debt, not
accepted risk. A baseline only stops old findings from failing the job; any new one fails it. At the start:
- bandit: 30, mostly string-built SQL (B608) in `app/lookup_shadow_import.py` and friends, a /tmp default in
  `scripts/cloud_env.py`, and the engine checks' own SHA-1 and urlopen use.
- checkov: 17, on Key Vault (public network, secret expiry and content type, and purge protection, which is a
  parameter defaulting to on that checkov cannot read), ACR (public network, image quarantine and scanning)
  and Storage (default network rule, public access, replication, a naming false positive). Most wait for the
  landing zone's private networking.

## Red on the first run

Two jobs fail on `main` today, on real findings. Do not baseline them without a decision:
- `dependency-audit`: filelock 3.32.3 has nine advisories newer than the three accepted in M6 (fix 4.2.1).
  Needs the security review's decision: accept with a reason in `security/accepted-advisories.txt`, or bump.
- `container-scan`: the pinned `python:3.12.12-slim-bookworm` base has 39 fixable HIGH/CRITICAL OS findings
  (openssl/libssl3, gnutls, krb5, pcre2, libcap2, perl-base). Fix: the approved base image, or a newer
  digest of the upstream tag, and a full image build to prove it.

## Proving a scan blocks (ac3)

On a throwaway branch, add one of these, open a pull request, and record the red job and the blocked merge
button on the task. Then close the pull request and delete the branch.

| Scan | Seed |
|---|---|
| `sast` | `app/_seed_sast.py` containing `import subprocess` and `subprocess.call(cmd, shell=True)` (B602) |
| `dependency-audit` | a line `requests==2.19.0` in `constraints.txt` |
| `secret-scan` | a file with `TOKEN = "ghp_` + 36 random letters and digits + `"` (a made-up GitHub token) |
| `iac-scan` | in `infra/azure/storage.bicep`, set `supportsHttpsTrafficOnly: false` and `minimumTlsVersion: 'TLS1_0'` |
| `container-scan` | change the `Dockerfile`'s `FROM` to `python:3.12-slim` (no digest) |

The same seeds were run locally against the tools on 29 Oct 2026; each exited 1.

## Branch protection (Tarkesh)

Settings → Branches needs admin rights on the repository. Required checks use the job names above plus
`test` from `ci.yml`; `15368` is the GitHub Actions app, so only Actions can satisfy them. Pull requests
need one approving review from someone other than the author.

While `dependency-audit` and `container-scan` are red on `main` (above), require the other four first,
or nothing can merge:

```bash
gh api -X PUT repos/muttonkodibiriyani/Invoice_studio_Al/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "checks": [
    {"context": "test", "app_id": 15368}, {"context": "sast", "app_id": 15368},
    {"context": "secret-scan", "app_id": 15368}, {"context": "iac-scan", "app_id": 15368}
  ]},
  "enforce_admins": true,
  "required_pull_request_reviews": {"required_approving_review_count": 1, "dismiss_stale_reviews": true},
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

Once both are green, add them:

```bash
gh api -X POST repos/muttonkodibiriyani/Invoice_studio_Al/branches/main/protection/required_status_checks/checks \
  --input - <<'JSON'
{"checks": [{"context": "dependency-audit", "app_id": 15368}, {"context": "container-scan", "app_id": 15368}]}
JSON
gh api repos/muttonkodibiriyani/Invoice_studio_Al/branches/main/protection \
  --jq '{checks: [.required_status_checks.checks[].context], reviews: .required_pull_request_reviews.required_approving_review_count}'
```

## Waiting for the landing zone

When the platform's landing zone exists, and not before:
1. A self-hosted runner inside the network (label `<LZ_RUNNER_LABEL>`) runs the image build and push to the
   private registry `<PRIVATE_ACR_LOGIN_SERVER>` over its private endpoint, in place of the hosted runner and
   public ACR in `deploy-azure.yml`.
2. On that runner, `trivy image` scans the built image by digest (all packages, HIGH/CRITICAL with a fix)
   before a revision is created, and the deploy stops on a finding. The image never needs to leave the network.
3. The base image becomes `<APPROVED_BASE_IMAGE>@<APPROVED_BASE_IMAGE_DIGEST>`, pulled from the private
   registry, and the platform's mandated scanners replace the tools above if it names others.
