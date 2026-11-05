"""container-scan's base image exception list: only the reviewed CVE, packages and digest pass, until expiry."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "trivy"
DIGEST = "sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
IMAGE = f"python:3.12.16-slim-bookworm@{DIGEST}"
TODAY = dt.date(2026, 11, 5)

spec = importlib.util.spec_from_file_location("check_trivy_exceptions",
                                              ROOT / "tools" / "security" / "check_trivy_exceptions.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def _report(name="base-apt-cve.json"):
    return json.loads((FIXTURES / name).read_text())


def _exceptions():
    return json.loads((ROOT / "tools" / "security" / "trivy-exceptions.json").read_text())


def _check(report=None, exceptions=None, image=IMAGE, today=TODAY):
    return gate.check({image: report or _report()}, exceptions or _exceptions(), today)


def test_the_checked_in_entry_is_the_reviewed_one():
    [entry] = _exceptions()["exceptions"]
    assert entry["cve"] == "CVE-2026-109518"
    assert entry["packages"] == ["apt", "libapt-pkg6.0"]
    assert entry["image_digest"] == DIGEST
    assert entry["expires"] == "2026-11-19"
    assert DIGEST in (ROOT / "Dockerfile").read_text()


def test_exactly_the_excepted_findings_pass_up_to_and_on_the_expiry_day():
    for today in (TODAY, dt.date(2026, 11, 19)):
        problems, notes = _check(today=today)
        assert problems == []
        assert len(notes) == 2 and all("excepted until 2026-11-19" in n for n in notes)


def test_a_clean_scan_passes_and_notes_the_unused_entry():
    problems, notes = _check(_report("base-clean.json"))
    assert problems == []
    assert any("matched no finding" in n for n in notes)


def test_an_extra_cve_fails():
    report = _report()
    report["Results"][0]["Vulnerabilities"].append(
        {"VulnerabilityID": "CVE-2026-1", "PkgName": "libssl3", "InstalledVersion": "3.0.17-1~deb12u2",
         "FixedVersion": "3.0.17-1~deb12u3", "Severity": "CRITICAL"})
    problems, _ = _check(report)
    assert len(problems) == 1 and "CVE-2026-1 in libssl3" in problems[0] and "not in the exception list" in problems[0]


def test_the_excepted_cve_on_another_package_fails():
    report = _report()
    extra = copy.deepcopy(report["Results"][0]["Vulnerabilities"][0])
    extra["PkgName"] = "apt-utils"
    report["Results"][0]["Vulnerabilities"].append(extra)
    problems, _ = _check(report)
    assert len(problems) == 1 and "CVE-2026-109518 in apt-utils" in problems[0]
    assert "does not cover this package" in problems[0]


def test_another_base_digest_fails():
    other = "sha256:" + "1" * 64
    image = f"python:3.12.16-slim-bookworm@{other}"
    report = _report()
    report["ArtifactName"] = image
    report["Metadata"]["RepoDigests"] = [f"python@{other}"]
    problems, _ = _check(report, image=image)
    assert any("no scanned image has" in p for p in problems)  # the entry is stale
    assert sum("does not cover this package and digest" in p for p in problems) == 2  # both findings fail


def test_a_report_of_another_digest_than_the_image_fails():
    report = _report()
    report["Metadata"]["RepoDigests"] = ["python@sha256:" + "2" * 64]
    problems, _ = _check(report)
    assert any("not " + DIGEST in p for p in problems)


def test_an_expired_entry_fails_even_with_no_finding():
    problems, _ = _check(today=dt.date(2026, 11, 20))
    assert any("expired on 2026-11-19" in p and p.startswith("exception ") for p in problems)
    assert sum("its exception expired" in p for p in problems) == 2
    problems, _ = _check(_report("base-clean.json"), today=dt.date(2026, 11, 20))
    assert problems and all("expired" in p for p in problems)


@pytest.mark.parametrize("field, value", [
    ("cve", "GHSA-xxxx"), ("packages", []), ("image_digest", "sha256:abc"), ("expires", "19 Nov"), ("reason", ""),
])
def test_an_invalid_entry_fails_and_excepts_nothing(field, value):
    exceptions = _exceptions()
    exceptions["exceptions"][0][field] = value
    problems, _ = _check(exceptions=exceptions)
    assert len(problems) == 3  # the invalid entry and both findings


def test_a_report_that_is_not_trivy_json_of_this_image_fails():
    assert _check({"Results": []})[0]
    report = _report()
    report["ArtifactName"] = "python:3.12-slim"
    assert _check(report)[0]


def test_the_command_line_exit_codes(tmp_path, capsys):
    clean = FIXTURES / "base-apt-cve.json"
    assert gate.main([f"{IMAGE}={clean}", "--today", "2026-11-05"]) == 0
    assert gate.main([f"{IMAGE}={clean}", "--today", "2026-11-20"]) == 1
    assert "::error" in capsys.readouterr().out
    assert gate.main([f"{IMAGE}={tmp_path / 'missing.json'}"]) == 2
