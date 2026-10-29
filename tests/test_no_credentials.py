"""No file git would commit carries a credential in a shape the providers issue (Google API keys, private keys,
GitHub, OpenAI, Anthropic, AWS, Azure storage, Slack and Stripe tokens). Keys belong in Secret Manager, the app's
settings or a local .env, never in the repository. A failure names the file, line and kind, never the value."""
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

KINDS = {
    "Google API key": r"AIza[0-9A-Za-z_\-]{35}",
    "Google OAuth client secret": r"GOCSPX-[0-9A-Za-z_\-]{28}",
    "private key": r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----",
    "service account key id": r"\"private_key_id\"\s*:\s*\"[0-9a-f]{40}\"",
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})",
    "OpenAI key": r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}T3BlbkFJ|\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{40,}",
    "Anthropic key": r"\bsk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_\-]{40,}",
    "AWS access key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "Azure storage key": r"AccountKey=[A-Za-z0-9+/]{80,}",
    "Slack token": r"\bxox[abposr]-[0-9A-Za-z\-]{10,}",
    "Stripe key": r"\b[rs]k_live_[0-9A-Za-z]{20,}",
}
NAMES = dict(enumerate(KINDS))
PATTERN = re.compile("|".join(f"(?P<k{i}>{rx})" for i, rx in enumerate(KINDS.values())))


def committable_files(root):
    """Tracked files plus untracked ones .gitignore lets through: what `git add -A` would commit."""
    if shutil.which("git") is None or not (root / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    ).stdout
    return sorted({name for name in out.decode("utf-8", "surrogateescape").split("\0") if name})


def findings(root, names):
    """`path:line: kind` for each credential-shaped string; binary and deleted files are skipped."""
    found = []
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            for match in PATTERN.finditer(line):
                found.append(f"{name}:{lineno}: {NAMES[int(match.lastgroup[1:])]}")
    return found


def test_no_committable_file_carries_a_credential():
    found = findings(ROOT, committable_files(ROOT))
    assert found == [], "credentials in the repository; remove them and rotate or restrict each key:\n" + "\n".join(found)


def test_each_kind_is_found_and_never_echoed(tmp_path):
    # Look-alike values are assembled at run time so this file never holds one.
    samples = {
        "Google API key": "AI" + "za" + "x" * 35,
        "Google OAuth client secret": "GOC" + "SPX-" + "x" * 28,
        "private key": "-----BEGIN " + "PRIVATE KEY-----",
        "service account key id": '"private_key_id": "' + "0" * 40 + '"',
        "GitHub token": "gh" + "p_" + "x" * 36,
        "OpenAI key": "sk-" + "proj-" + "x" * 40,
        "Anthropic key": "sk-" + "ant-api03-" + "x" * 40,
        "AWS access key": "AK" + "IA" + "X" * 16,
        "Azure storage key": "Account" + "Key=" + "x" * 80,
        "Slack token": "xo" + "xb-" + "1" * 10,
        "Stripe key": "sk" + "_live_" + "x" * 20,
    }
    assert set(samples) == set(KINDS)
    names = []
    for i, (kind, value) in enumerate(samples.items()):
        (tmp_path / f"f{i}.txt").write_text(f"before\nsetting = '{value}'\n")
        names.append(f"f{i}.txt")
    (tmp_path / "binary.bin").write_bytes(b"\0" + samples["Google API key"].encode())
    found = findings(tmp_path, names + ["binary.bin", "gone.txt"])
    assert found == [f"f{i}.txt:2: {kind}" for i, kind in enumerate(samples)]
    assert not any(value in line for value in samples.values() for line in found)
