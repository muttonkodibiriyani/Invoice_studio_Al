"""The helpers CI's security scans use: the base image pin check and the dependency audit input."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "0" * 64


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "security" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pins = _load("check_base_image")
audit = _load("audit_requirements")


@pytest.mark.parametrize("dockerfile", [
    f"FROM python:3.12-slim@{DIGEST}\n",
    f"FROM registry.example/team/base@{DIGEST} AS build\nFROM build\nRUN true\n",
    f"ARG BASE=python@{DIGEST}\nFROM ${{BASE}}\n",
    "ARG BASE\nFROM ${BASE}\n",
    f"FROM --platform=linux/amd64 python@{DIGEST}\nFROM scratch\n",
    f"# FROM python:latest in a comment\nFROM \\\n    python@{DIGEST}\n",
])
def test_a_base_pinned_by_digest_passes(dockerfile):
    problems, _ = pins.check_dockerfile(dockerfile)
    assert problems == []


@pytest.mark.parametrize("dockerfile", [
    "FROM python:3.12-slim\n",
    "FROM python\n",
    "from python:latest\n",
    f"FROM python@{DIGEST[:-1]}\n",
    "ARG BASE=python:3.12\nFROM ${BASE}\n",
    "FROM $BASE\n",
    f"FROM python@{DIGEST} AS build\nFROM node:22\n",
])
def test_a_base_not_pinned_by_digest_fails(dockerfile):
    problems, _ = pins.check_dockerfile(dockerfile)
    assert problems


def test_a_build_arg_value_is_checked():
    text = "ARG BASE\nFROM ${BASE}\n"
    assert pins.check_dockerfile(text, {"BASE": "inv-studio:latest"})[0]
    assert pins.check_dockerfile(text, {"BASE": f"inv-studio@{DIGEST}"}) == ([], [f"inv-studio@{DIGEST}"])


def test_the_repository_dockerfiles_are_pinned_and_name_the_approved_image_placeholder():
    for name in ("Dockerfile", "Dockerfile.app-layer"):
        assert pins.main([str(ROOT / name)]) == 0
    problems, images = pins.check_dockerfile((ROOT / "Dockerfile").read_text())
    assert problems == [] and len(images) == 1 and "@sha256:" in images[0]
    # The platform has not named the approved image; the placeholder stays until it does (never invented).
    assert "<APPROVED_BASE_IMAGE>@<APPROVED_BASE_IMAGE_DIGEST>" in (ROOT / "Dockerfile").read_text()


def test_the_audit_list_takes_constraints_and_the_dockerfile_pins_without_local_suffixes():
    constraints = "# comment\nfilelock==3.32.3\ntorch==2.14.1+cpu\nnumpy==2.3.5\n\n"
    dockerfile = (
        "ARG TORCH_VERSION=2.14.1+cpu\nARG NUMPY_VERSION=2.3.5\n"
        "RUN python -m pip install \\\n    pip==26.2.2 setuptools==84.2.1 wheel==0.48.0 \\\n"
        '    && python -m pip install "torch==${TORCH_VERSION}" "numpy==${NUMPY_VERSION}"\n'
        "RUN python - <<'PY'\nassert hashlib.sha256(b'').hexdigest()==os.environ['X']\nPY\n"
    )
    assert audit.requirements(constraints, dockerfile) == [
        "filelock==3.32.3", "torch==2.14.1", "numpy==2.3.5", "pip==26.2.2", "setuptools==84.2.1", "wheel==0.48.0"]


def test_the_repository_audit_list_covers_every_constraint_and_the_packaging_tools():
    constraints = (ROOT / "constraints.txt").read_text()
    listed = audit.requirements(constraints, (ROOT / "Dockerfile").read_text())
    pinned = [line for line in constraints.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert len(audit.constraint_pins(constraints)) == len(pinned), "a constraints.txt line is not name==version"
    names = {line.split("==")[0].lower() for line in listed}
    assert {"pip", "setuptools", "wheel", "torch", "torchvision"} <= names
    assert not any("+" in line for line in listed)


def test_accepted_advisories_become_ignore_arguments():
    text = (ROOT / "security" / "accepted-advisories.txt").read_text()
    ids = audit.accepted_ids(text)
    assert ids and all(re.fullmatch(r"(GHSA(-[0-9a-z]{4}){3}|CVE-\d{4}-\d+|PYSEC-\d{4}-\d+)", i) for i in ids)


def test_the_scan_baselines_are_valid_and_record_only_known_debt():
    bandit = json.loads((ROOT / "security" / "bandit-baseline.json").read_text())
    files = {r["filename"] for r in bandit["results"]}
    assert files and all(f.startswith(("app/", "scripts/", "tools/security/check_")) for f in files)
    checkov = json.loads((ROOT / "security" / "checkov-baseline.json").read_text())
    assert [f["file"] for f in checkov["failed_checks"]] == ["/main.json"]
