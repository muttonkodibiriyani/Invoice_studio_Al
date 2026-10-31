"""Static checks on the security workflow: when it runs, what it may do, and that it matches its doc."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"
DOC = ROOT / "docs" / "CI_SECURITY.md"
JOBS = {"sast", "dependency-audit", "secret-scan", "iac-scan", "container-scan"}


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_it_runs_on_every_pull_request_and_on_pushes_to_main():
    triggers = _workflow()[True]  # YAML 1.1 reads the key "on" as True
    assert triggers["pull_request"] is None, "no branch or path filter: a required check must always run"
    assert triggers["push"] == {"branches": ["main"]}


def test_the_token_can_only_read_and_no_job_asks_for_more():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job, f"{name} widens the token"
    assert "secrets." not in WORKFLOW.read_text()


def test_the_jobs_are_the_required_checks_and_every_one_has_a_time_limit():
    jobs = _workflow()["jobs"]
    assert set(jobs) == JOBS
    for name, job in jobs.items():
        assert job["name"] == name, "the check name branch protection requires is the job name"
        assert job["timeout-minutes"] <= 20
        assert job.get("continue-on-error") is not True
        for step in job["steps"]:
            assert step.get("continue-on-error") is not True, f"{name} lets a finding pass"
            assert "|| true" not in step.get("run", ""), f"{name} lets a finding pass"


def test_actions_are_pinned_by_commit_and_tools_by_version_and_checksum():
    workflow = _workflow()
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
            if "checkout" in step.get("uses", ""):
                assert step["with"]["persist-credentials"] is False
    env = workflow["env"]
    for key, value in env.items():
        if key.endswith("_SHA256"):
            assert re.fullmatch(r"[0-9a-f]{64}", value), key
        elif key.endswith("_VERSION"):
            assert re.fullmatch(r"v?\d+\.\d+\.\d+", value), key
    text = WORKFLOW.read_text()
    for tool in ("gitleaks", "trivy"):
        assert f'echo "${{{tool.upper()}_SHA256}}' in text, f"{tool} is not checksum-verified"
    for tool, var in (("bandit", "BANDIT"), ("pip-audit", "PIP_AUDIT"), ("checkov", "CHECKOV")):
        assert f'--from "{tool}==${{{var}_VERSION}}"' in text


def test_each_scan_fails_on_findings_against_its_reviewed_baseline():
    text = WORKFLOW.read_text()
    assert "-b security/bandit-baseline.json" in text
    assert "--ignore-args" in text and "--disable-pip" in text
    assert "fetch-depth: 0" in text and "gitleaks\" git " in text
    assert "--baseline security/checkov-baseline.json" in text and "az bicep build" in text
    assert "check_base_image.py" in text
    assert text.count("--exit-code 1") == 2


def test_the_doc_names_every_job_tool_version_and_the_branch_protection_checks():
    doc = DOC.read_text()
    env = _workflow()["env"]
    for job in JOBS:
        assert f"`{job}`" in doc
    for key in ("BANDIT_VERSION", "PIP_AUDIT_VERSION", "CHECKOV_VERSION", "GITLEAKS_VERSION", "TRIVY_VERSION"):
        assert env[key].lstrip("v") in doc, key
    required = set(re.findall(r'"context": "([\w-]+)"', doc))
    assert required == JOBS | {"test"}
