"""Offline script checks using Node DOM/SDK doubles; no external browser packages."""
import hashlib
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node is needed for the offline browser script checks")
@pytest.mark.parametrize("scenario", [
    "sign-in", "redirect-error", "no-role", "users", "batch-upload", "target-chip", "reader-marks", "bundle",
])
def test_browser_scripts(scenario):
    script = "entra-auth.bundle.js" if scenario == "bundle" else "app.js"
    result = subprocess.run(
        [NODE, str(ROOT / "tests/browser_review.cjs"), str(ROOT / "app/static" / script), scenario],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_sign_in_page_offers_only_microsoft():
    page = (ROOT / "app/static/index.html").read_text()
    assert page.count('id="microsoft-sign-in"') == 1 and "Sign in with Microsoft" in page
    for gone in ("google-sign-in", "password-sign-in", "auth-password", "Forgot password", "auth-send-verification",
                 "user-create-form", "user-link-dialog", "set-password", "firebase"):
        assert gone not in page, gone
    assert '<script src="/static/entra-auth.bundle.js" defer></script>' in page


def test_no_firebase_set_password_or_hosting_files_remain():
    static = sorted(path.name for path in (ROOT / "app/static").iterdir())
    assert static == ["app.js", "entra-auth-entry.js", "entra-auth.bundle.js", "index.html", "style.css"]
    for gone in ("hosting", "scripts/firebase-bundle", "scripts/hosting_release.py", "scripts/sync_static.py"):
        assert not (ROOT / gone).exists(), gone
    assert "firebase" not in (ROOT / "app/static/app.js").read_text().casefold()


def test_the_entra_bundle_matches_its_recipe():
    recorded, name = (ROOT / "scripts/entra-bundle/bundle.sha256").read_text().split()
    assert name == "entra-auth.bundle.js"
    assert hashlib.sha256((ROOT / "app/static" / name).read_bytes()).hexdigest() == recorded, (
        "entra-auth.bundle.js changed without its recipe; rebuild it with npm ci && npm run build in scripts/entra-bundle")
    recipe = json.loads((ROOT / "scripts/entra-bundle/package.json").read_text())
    lock = json.loads((ROOT / "scripts/entra-bundle/package-lock.json").read_text())
    for package, version in recipe["devDependencies"].items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"{package} is not pinned"
        assert lock["packages"][f"node_modules/{package}"]["version"] == version


def test_the_sign_in_page_works_with_the_no_inline_csp():
    elements = {}

    class NoInline(HTMLParser):
        inline_script = False

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if "id" in attributes:
                elements[attributes["id"]] = attributes
            assert "style" not in attributes, "inline styles are blocked by the declared CSP"
            assert not any(key.startswith("on") for key in attributes), "inline handlers are blocked by CSP"
            if tag == "script":
                self.inline_script = "src" not in attributes
            assert tag != "style", "inline stylesheets are blocked by CSP"

        def handle_endtag(self, tag):
            if tag == "script":
                self.inline_script = False

        def handle_data(self, data):
            assert not (self.inline_script and data.strip()), "inline scripts are blocked by CSP"

    NoInline().feed((ROOT / "app/static/index.html").read_text())
    for element_id in ("auth-message", "auth-detail"):
        assert elements[element_id]["role"] == "status"
        assert elements[element_id]["aria-live"] == "polite"
    assert elements["microsoft-sign-in"]["type"] == "button"
