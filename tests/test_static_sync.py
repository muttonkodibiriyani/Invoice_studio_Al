"""hosting/public (what Firebase Hosting serves) is derived from app/static (where the UI is edited):
the repo never drifts, and the sync script repairs any drift it is shown."""
import hashlib
import importlib.util
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sync_static", ROOT / "scripts" / "sync_static.py")
sync_static = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_static)


def test_the_repo_hosting_copy_matches_app_static():
    stale, orphans = sync_static.drift(ROOT)
    assert stale == [], "hosting/public is out of date; run scripts/sync_static.py"
    assert orphans == [], "hosting/public/static carries files app/static does not"


def test_pages_land_in_public_and_assets_in_public_static():
    pairs = {s.name: t.relative_to(ROOT).as_posix() for s, t in sync_static.mapping(ROOT).items()}
    assert pairs["index.html"] == "hosting/public/index.html"
    assert pairs["set-password.html"] == "hosting/public/set-password.html"
    assert pairs["app.js"] == "hosting/public/static/app.js"
    assert "firebase-auth-entry.js" not in pairs, "the bundle source is never published"
    assert pairs["firebase-auth.bundle.js"] == "hosting/public/static/firebase-auth.bundle.js"


def tree(tmp_path):
    copy = tmp_path / "repo"
    (copy / "app").mkdir(parents=True)
    shutil.copytree(ROOT / "app" / "static", copy / "app" / "static")
    shutil.copytree(ROOT / "hosting", copy / "hosting")
    return copy


def test_an_edited_ui_file_is_drift_and_the_sync_repairs_it(tmp_path, capsys):
    copy = tree(tmp_path)
    (copy / "app" / "static" / "app.js").write_text("changed()\n")
    assert sync_static.main(["--check"], root=copy) == 1
    assert "out of date: hosting/public/static/app.js" in capsys.readouterr().out
    assert sync_static.main([], root=copy) == 0
    assert (copy / "hosting" / "public" / "static" / "app.js").read_text() == "changed()\n"
    assert sync_static.main(["--check"], root=copy) == 0


def test_a_file_only_in_hosting_is_reported_not_silently_kept(tmp_path, capsys):
    copy = tree(tmp_path)
    (copy / "hosting" / "public" / "static" / "stray.js").write_text("stray")
    assert sync_static.main(["--check"], root=copy) == 1
    assert "not derived from app/static: hosting/public/static/stray.js" in capsys.readouterr().out


def test_the_firebase_bundle_is_the_one_its_recipe_records():
    recorded, name = (ROOT / "scripts" / "firebase-bundle" / "bundle.sha256").read_text().split()
    bundle = ROOT / "app" / "static" / name
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == recorded, (
        "firebase-auth.bundle.js changed without its recipe; rebuild it as docs/RECOVERY.md says")
