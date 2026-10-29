"""Offline script checks using Node DOM/SDK doubles; no external browser packages."""
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
COPIES = ["app/static", "hosting/public/static"]


@pytest.mark.skipif(NODE is None, reason="Node is needed for the offline browser script checks")
@pytest.mark.parametrize("copy", COPIES)
@pytest.mark.parametrize("scenario", [
    "success", "missing", "query-code", "fragment-and-query", "module-missing", "module-fails",
    "expired", "invalid", "disabled", "network", "verify-transient-success", "verify-transient-exhaust",
    "verify-other-auth-error", "confirm-expired", "confirm-disabled", "weak", "confirm-transient-success",
    "confirm-transient-exhaust", "confirm-module-missing", "users", "forgot", "batch-upload",
    "target-chip", "reader-marks",
])
def test_password_and_users_scripts(copy, scenario):
    script = "app.js" if scenario in ("users", "forgot", "batch-upload", "target-chip", "reader-marks") else "set-password.js"
    result = subprocess.run(
        [NODE, str(ROOT / "tests/browser_password_review.cjs"), str(ROOT / copy / script), scenario],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("asset", ["index.html", "set-password.html", "static/set-password.js", "static/firebase-auth.bundle.js",
                                   "static/app.js", "static/style.css"])
def test_password_assets_match_the_hosting_copies(asset):
    source = ROOT / "app/static" / Path(asset).name
    hosted = ROOT / "hosting/public" / asset
    assert source.read_bytes() == hosted.read_bytes()


@pytest.mark.parametrize("page", ["app/static/index.html", "app/static/set-password.html", "hosting/public/index.html", "hosting/public/set-password.html"])
def test_auth_pages_work_with_the_no_inline_csp(page):
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

    NoInline().feed((ROOT / page).read_text())
    prefix = "sp" if "set-password" in page else "auth"
    for element_id in (f"{prefix}-message", f"{prefix}-detail"):
        assert elements[element_id]["role"] == "status"
        assert elements[element_id]["aria-live"] == "polite"
    if prefix == "sp":
        assert elements["sp-message"]["tabindex"] == "-1"
        assert elements["sp-retry"]["type"] == "button"
        assert "hidden" in elements["sp-retry"]
