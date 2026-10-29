"""The local install is configured from one env list, and the install/run scripts exist on both
platforms and agree with it."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / ".env.example").read_text()
# Every name the application itself reads. Keep the pattern in step with the
# prefixes used in app/.
READS = re.compile(r'"(INV_[A-Z0-9_]+|VERTEX_[A-Z0-9_]+|GOOGLE_CLOUD_PROJECT)"')


def test_every_variable_the_app_reads_is_in_the_one_shared_list():
    """A variable missing here is a setting an operator cannot discover."""
    names = {match.group(1) for path in (ROOT / "app").rglob("*.py")
             for match in READS.finditer(path.read_text())}
    assert "INV_STUDIO_DATA" in names and "INV_STUDIO_CLOUD" in names
    for name in sorted(names):
        assert name in EXAMPLE, f"{name} is read by the app but absent from .env.example"


def test_both_run_scripts_load_the_env_file_and_an_exported_variable_wins():
    run_sh = (ROOT / "scripts" / "run.sh").read_text()
    run_cmd = (ROOT / "scripts" / "run.cmd").read_text()
    for script in (run_sh, run_cmd):
        assert ".env" in script
        # An already-set variable wins over the file in both shells.
        assert "INV_STUDIO_PORT" in script
    assert "127.0.0.1" in run_sh and "127.0.0.1" in run_cmd, "the local server binds loopback only"


def test_both_setup_scripts_write_env_from_the_example_and_install_the_tests():
    for setup in (ROOT / "scripts" / "setup.sh", ROOT / "scripts" / "setup.cmd"):
        text = setup.read_text()
        assert ".env.example" in text
        assert "[test]" in text


def test_the_shell_scripts_parse():
    for script in ("setup.sh", "run.sh"):
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / script)], check=True)


def test_windows_batch_files_reach_windows_with_crlf_endings():
    attributes = (ROOT / ".gitattributes").read_text()
    assert "*.cmd text eol=crlf" in attributes


def test_the_example_carries_no_values():
    """Leaving .env as the copied example must mean plain local mode."""
    values = {}
    for line in EXAMPLE.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", key), f"not a setting: {line!r}"
        if value:
            values[key] = value
    # Every setting is named and blank: the run scripts default the port to 8765.
    assert values == {}


def test_the_secret_manager_credentials_are_named_but_never_given_values():
    for secret in ("INV_STUDIO_DATABASE_URL", "INV_STUDIO_VAULT_KEY"):
        assert secret in EXAMPLE, f"{secret} should be documented"
        assert not re.search(rf"^{secret}=", EXAMPLE, re.M), f"{secret} belongs in Secret Manager, not .env"


def test_the_readme_references_only_files_that_exist():
    readme = (ROOT / "README.md").read_text()
    for target in re.findall(r"\]\(([^)#]+)\)", readme):
        if target.startswith(("http://", "https://")):
            continue
        assert (ROOT / target).exists(), f"README links to {target}, which does not exist"
    # Paths the README asks the reader to run or edit.
    for path in ("scripts/setup.sh", "scripts/run.sh", "scripts/setup.cmd", "scripts/run.cmd",
                 "scripts/sync_static.py", ".env.example", ".github/workflows/ci.yml"):
        assert (ROOT / path).exists(), path
        assert path.split("/")[-1] in readme, f"{path} should be documented"
    assert "docker compose up" in readme and (ROOT / "docker-compose.yml").exists()
