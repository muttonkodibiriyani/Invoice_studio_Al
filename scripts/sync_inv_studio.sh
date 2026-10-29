#!/usr/bin/env bash
# Bring an Inv_studio release into this repository. Usage:
#   scripts/sync_inv_studio.sh [--check] <inv_studio-tag-or-commit>
# The Azure changes are every file this branch differs in from the upstream
# commit last synced (.inv-studio-sync): the import, Entra sign-in, Citadel AI,
# deploy kit, docs. The script applies the upstream diff since that commit,
# stages it and records the new upstream commit. It lists the files both
# sides changed (OVERLAP), which need a by-hand check that the Azure behaviour
# survived. Upstream changes to files Azure deleted are skipped and listed
# (SKIPPED). It also confirms that every file that still differs from upstream
# is an Azure change. --check does the listing only and changes nothing.
# Nothing is committed or pushed.
set -euo pipefail
cd "$(dirname "$0")/.."

check=0
[ "${1:-}" = --check ] && { check=1; shift; }
ref=${1:?usage: scripts/sync_inv_studio.sh [--check] <inv_studio-tag-or-commit>}
url=${INV_STUDIO_URL:-https://github.com/muttonkodibiriyani/Inv_studio.git}

git diff --quiet && git diff --cached --quiet || { echo "working tree not clean" >&2; exit 1; }
git remote get-url inv_studio >/dev/null 2>&1 || git remote add inv_studio "$url"
git fetch -q --tags inv_studio main

base=$(tr -d '[:space:]' < .inv-studio-sync)
target=$(git rev-parse "$ref^{commit}")
git merge-base --is-ancestor "$base" "$target" || { echo "$ref does not descend from $base" >&2; exit 1; }

azure=$(git diff --name-only "$base" HEAD)
upstream=$(git diff --name-only "$base" "$target")
overlap=$(comm -12 <(sort <<<"$azure") <(sort <<<"$upstream") | sed '/^$/d')
# A file Azure deleted (Firebase hosting, password links...) stays deleted.
deleted=$(comm -12 <(git diff --name-only --diff-filter=D "$base" HEAD | sort) <(sort <<<"$upstream") | sed '/^$/d')

echo "upstream $base..$target ($ref): $(sed '/^$/d' <<<"$upstream" | wc -l) files"
echo "Azure changes since $base: $(sed '/^$/d' <<<"$azure" | wc -l) files"
if [ -n "$overlap" ]; then
  echo "OVERLAP: changed by both upstream and Azure. Keep the Azure behaviour and say how in the PR:"
  sed 's/^/  /' <<<"$overlap"
else
  echo "OVERLAP: none"
fi
if [ -n "$deleted" ]; then
  echo "SKIPPED: deleted in Azure, so the upstream change is not applied. Carry it to the Azure replacement or say in the PR why it does not apply:"
  sed 's/^/  /' <<<"$deleted"
fi
[ "$check" = 1 ] && exit 0
[ "$base" = "$target" ] && { echo "already synced to $ref"; exit 0; }

excludes=()
while IFS= read -r f; do [ -n "$f" ] && excludes+=("--exclude=$f"); done <<<"$deleted"
if ! git diff --binary "$base" "$target" | git apply --3way --index "${excludes[@]}"; then
  echo "apply stopped: resolve the conflicts above (a file Azure deleted stays deleted unless the PR says otherwise), then re-run the checks" >&2
  exit 1
fi
echo "$target" > .inv-studio-sync
git add .inv-studio-sync

# Every file that still differs from upstream must be an Azure change.
stray=$(comm -23 <(git diff --name-only "$target" "$(git write-tree)" | sort) \
                 <(printf '%s\n.inv-studio-sync\n' "$azure" | sort) | sed '/^$/d')
if [ -n "$stray" ]; then
  echo "NOT APPLIED: these files differ from $ref but were not Azure changes:" >&2
  sed 's/^/  /' <<<"$stray" >&2
  exit 1
fi
echo "ok: the tree is $ref plus the Azure changes only"
