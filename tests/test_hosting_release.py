"""The Hosting release script: firebase.json (CLI form) must serve exactly what the live version serves, plus the
set-password page; previews follow the candidate tag; the live channel needs an explicit flag and never a tag."""
import gzip
import hashlib
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hosting_release", ROOT / "scripts" / "hosting_release.py")
hosting_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosting_release)


def live_config():
    return json.loads((ROOT / "docs" / "hosting-version-8e2243be6414c5f4.json").read_text())["config"]


def test_repo_config_is_the_live_config_plus_the_password_page():
    _, ignore, config = hosting_release.load_config(ROOT / "hosting" / "firebase.json")
    live = live_config()
    assert config["headers"] == live["headers"]
    added = {"glob": "/set-password", "path": "/set-password.html"}
    assert config["rewrites"] == live["rewrites"][:-1] + [added] + live["rewrites"][-1:]
    assert ignore == ["firebase.json", "**/.*"]


def test_the_rest_form_recorded_from_the_live_version_reads_the_same(tmp_path):
    (tmp_path / "firebase.json").write_text(json.dumps({"hosting": {"public": "public", **live_config()}}))
    assert hosting_release.load_config(tmp_path / "firebase.json")[2] == live_config()


def test_firebase_json_uses_the_cli_form():
    hosting = json.loads((ROOT / "hosting" / "firebase.json").read_text())["hosting"]
    assert all(isinstance(rule["headers"], list) and "source" in rule for rule in hosting["headers"])
    assert all("source" in rule and "glob" not in rule and "path" not in rule for rule in hosting["rewrites"])


def test_files_are_hashed_gzipped_and_dotfiles_skipped(tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "a.js").write_text("one")
    (tmp_path / ".hidden").write_text("no")
    (tmp_path / "static" / ".map").write_text("no")
    (tmp_path / "firebase.json").write_text("{}")
    files = hosting_release.collect(tmp_path, ["firebase.json", "**/.*"])
    assert list(files) == ["/static/a.js"]
    digest, packed = files["/static/a.js"]
    assert gzip.decompress(packed) == b"one" and digest == hashlib.sha256(packed).hexdigest()
    assert hosting_release.collect(tmp_path, [])["/static/a.js"][0] == digest, "gzip output is deterministic"


@pytest.mark.parametrize("argv, message", [
    (["release", "live"], "needs both"),
    (["release", "preview-x", "--live"], "needs both"),
    (["release", "live", "--live", "--tag", "candidate"], "traffic split"),
    (["channel", "live"], "not a preview"),
])
def test_the_live_channel_needs_an_explicit_flag_and_never_a_tag(argv, message, capsys):
    with pytest.raises(SystemExit) as stopped:
        hosting_release.main(argv)
    assert message in str(stopped.value)


def test_a_preview_sends_every_service_rewrite_to_the_candidate_tag(capsys):
    hosting_release.main(["release", "users-candidate", "--tag", "candidate", "--dry-run"])
    output = capsys.readouterr().out
    config = json.loads(output[:output.rindex("}") + 1])
    service = [rule for rule in config["rewrites"] if "run" in rule]
    assert [rule["glob"] for rule in service] == ["/api/**", "/health", "/auth/**"]
    assert all(rule["run"] == {"serviceId": "inv-studio-api", "region": "us-central1", "tag": "candidate"} for rule in service)
    assert {"glob": "/set-password", "path": "/set-password.html"} in config["rewrites"]
