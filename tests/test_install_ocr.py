"""scripts/install_ocr.py gives each supported computer the scan readers the cloud image runs, at the
cloud image's versions, and refuses the rest with a reason. The setup scripts and README offer it.

scripts/check_engines.py then proves the install on the samples. A reader whose models the network
would not let it download is reported as that, not as a bad read, and does not fail an install that
left another scan reader working."""
import importlib.util
import pathlib
import subprocess
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install_ocr = load("install_ocr")
check_engines = load("check_engines")
CONSTRAINTS = (ROOT / "constraints.txt").read_text()
PINS = install_ocr.pins(CONSTRAINTS)
OPTIONAL = install_ocr.extras()
WINDOWS = install_ocr.plan("Windows", "AMD64")
LINUX = install_ocr.plan("Linux", "x86_64")
APPLE_14 = install_ocr.plan("Darwin", "arm64", "14.6.1")
APPLE_13 = install_ocr.plan("Darwin", "arm64", "13.7")
INTEL_12 = install_ocr.plan("Darwin", "x86_64", "12.7.6")


def steps(chosen, installer="pip"):
    return install_ocr.commands(chosen, PINS, OPTIONAL, installer, "python", "c.txt")


def kept(chosen):
    return set(install_ocr.constraints_for(chosen, CONSTRAINTS).splitlines())


def test_windows_linux_and_apple_silicon_on_macos_14_get_both_readers():
    for chosen in (WINDOWS, LINUX, APPLE_14):
        assert chosen["readers"] == ["paddleocr", "docling"]
        assert chosen["paddle"] is None, "the cloud image's paddlepaddle"
    assert WINDOWS["torch"] == LINUX["torch"] == "cpu-index"
    assert APPLE_14["torch"] == "pypi", "PyPI's macOS PyTorch is the CPU build"


def test_older_apple_silicon_and_intel_macs_get_paddleocr_and_are_told_why_not_docling():
    for chosen in (APPLE_13, INTEL_12):
        assert chosen["readers"] == ["paddleocr"]
        assert chosen["torch"] is None
        assert "PyTorch" in chosen["skipped"]["docling"]
    assert install_ocr.plan("Darwin", "arm64", "11.0")["readers"] == ["paddleocr"]
    assert INTEL_12["paddle"] == install_ocr.INTEL_MAC_PADDLE == "3.0.0"
    assert install_ocr.plan("Darwin", "x86_64", "15.1")["paddle"] == "3.0.0"


@pytest.mark.parametrize("computer, says", [
    (("Darwin", "x86_64", "11.7.10"), "macOS 12"),
    (("Darwin", "arm64", "10.16"), "macOS 11"),
    (("Darwin", "x86_64", "14.4", True), "Rosetta"),
    (("Windows", "ARM64"), "64-bit Windows"),
    (("Windows", "AMD64", "", False, (3, 12), 32), "64-bit Windows"),
    (("Linux", "aarch64"), "x86_64"),
    (("FreeBSD", "amd64"), "FreeBSD"),
    (("Windows", "AMD64", "", False, (3, 13)), "Python 3.12"),
])
def test_a_computer_without_a_reader_build_is_refused_with_the_reason(computer, says):
    chosen = install_ocr.plan(*computer)
    assert set(chosen) == {"refuse"}
    assert says in chosen["refuse"]


def test_windows_and_linux_keep_every_cloud_pin():
    lines = set(CONSTRAINTS.splitlines())
    assert kept(WINDOWS) == kept(LINUX) == lines
    assert "torch==2.14.1+cpu" in lines and "paddlepaddle==3.3.1" in lines


def test_macs_keep_the_cloud_pins_except_the_builds_they_cannot_use():
    lines = set(CONSTRAINTS.splitlines())
    gone = lines - kept(INTEL_12)
    assert {line.split("==")[0] for line in gone} == {
        "torch", "torchvision", "paddlepaddle", "cryptography", "pypdfium2"}
    assert "paddlex==3.7.2" in kept(INTEL_12) and "numpy==2.3.5" in kept(INTEL_12)
    apple = kept(APPLE_14)
    assert "torch==2.14.1" in apple and "torchvision==0.29.1" in apple
    assert not any("+cpu" in line or line.startswith(("cryptography==", "pypdfium2==")) for line in apple)


def test_windows_installs_cpu_pytorch_then_the_readers_then_headless_opencv_like_the_dockerfile():
    (label, torch), (_, readers), (_, opencv), (_, check) = steps(WINDOWS)
    assert "CPU-only" in label
    assert torch[torch.index("--index-url") + 1] == install_ocr.CPU_INDEX
    assert torch[-2:] == ["torch==2.14.1+cpu", "torchvision==0.29.1+cpu"]
    assert readers[-3:] == ["paddleocr==3.7.0", "paddlepaddle==3.3.1", "docling==2.143.1"]
    assert readers[-3:] == [*OPTIONAL["paddle"], *OPTIONAL["docling"]]
    for command in (torch, readers):
        assert command[command.index("--constraint") + 1] == "c.txt"
    assert {"--force-reinstall", "--no-deps"} <= set(opencv)
    assert opencv[-2:] == [f"numpy=={PINS['numpy']}", f"opencv-python-headless=={PINS['opencv-python-headless']}"]
    assert check == ["python", "-m", "pip", "check"]
    for command in (torch, readers, opencv):
        assert command[command.index("--only-binary") + 1] == ":all:", "never a source build"


def test_the_intel_mac_asks_for_paddlepaddle_3_0_0_and_nothing_from_pytorch_or_docling():
    commands = steps(INTEL_12, "uv")
    assert [label for label, _ in commands] == [
        "Installing PaddleOCR (several minutes the first time)", "Checking that the installed packages agree"]
    readers = commands[0][1]
    assert readers[:5] == ["uv", "pip", "install", "--python", "python"]
    assert readers[-2:] == ["paddleocr==3.7.0", "paddlepaddle==3.0.0"]
    assert not any(part.startswith(("torch", "docling")) for part in readers)
    assert commands[1][1] == ["uv", "pip", "check", "--python", "python"]


def test_apple_silicon_takes_the_pytorch_pypi_ships_for_macs():
    torch = steps(APPLE_14, "uv")[0][1]
    assert "--index-url" not in torch
    assert torch[-2:] == ["torch==2.14.1", "torchvision==0.29.1"]


def test_the_reader_pins_match_the_cloud_images():
    for requirement in OPTIONAL["paddle"] + OPTIONAL["docling"]:
        name, version = requirement.split("==")
        assert PINS[install_ocr.normal(name)] == version, f"pyproject.toml and constraints.txt differ on {name}"


def test_the_dry_run_prints_a_plan_or_the_reason_for_this_computer():
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "install_ocr.py"), "--dry-run"],
                            capture_output=True, text=True, cwd=ROOT, timeout=60)
    if result.returncode:
        assert result.stderr.startswith("Scan readers not installed: ")
    else:
        assert "==> Scan readers for this computer: PaddleOCR" in result.stdout
        assert "check_engines.py invoice2data --any paddleocr" in result.stdout


def test_check_engines_names_the_readers_it_knows(capsys):
    assert check_engines.main(["tesseract"]) == 2
    assert "invoice2data, paddleocr, docling" in capsys.readouterr().err
    assert check_engines.main(["invoice2data", "--any", "tesseract"]) == 2


def test_readers_before_any_are_required_and_the_ones_after_it_are_a_group():
    assert check_engines.split([]) == ([], [])
    assert check_engines.split(["invoice2data"]) == (["invoice2data"], [])
    assert check_engines.split(["invoice2data", "--any", "paddleocr", "docling"]) == (
        ["invoice2data"], ["paddleocr", "docling"])
    assert check_engines.split(["--any", "paddleocr"]) == ([], ["paddleocr"])


@pytest.mark.parametrize("error", [
    "huggingface_hub.errors.LocalEntryNotFoundError: An error happened while trying to locate the file",
    "ConnectError: [WinError 10013] An attempt was made to access a socket in a way forbidden",
    "urllib3.exceptions.MaxRetryError: HTTPSConnectionPool(host='huggingface.co', port=443): "
    "Max retries exceeded",
    "ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED",
    "socket.gaierror: [Errno -3] Temporary failure in name resolution",
])
def test_a_model_download_that_the_network_refused_is_not_reported_as_a_bad_read(error):
    assert check_engines.could_not_download([error])
    told = "\n".join(check_engines.why_blocked("Docling", "docling"))
    assert "refused its model download" in told and "No reinstall is needed" in told
    assert "check_engines.py --any docling" in told, "the way back in, without reinstalling"


@pytest.mark.parametrize("error", [
    "ValueError: cannot identify image file",
    "RuntimeError: Library cudnn is not found",
    "ImportError: DLL load failed while importing paddle",
])
def test_a_reader_that_broke_on_the_document_is_still_a_failure(error):
    assert not check_engines.could_not_download([error])


def test_both_setup_scripts_and_the_readme_offer_the_scan_readers():
    setup_sh = (ROOT / "scripts" / "setup.sh").read_text()
    setup_cmd = (ROOT / "scripts" / "setup.cmd").read_text()
    readme = (ROOT / "README.md").read_text()
    assert "--ocr" in setup_sh and "scripts/install_ocr.py" in setup_sh
    assert "--ocr" in setup_cmd and r"scripts\install_ocr.py" in setup_cmd
    for name in ("setup.sh --ocr", r"setup.cmd --ocr", "scripts/check_engines.py", "scripts/install_ocr.py"):
        assert name in readme, name


def test_the_reader_process_is_given_the_proxy_settings_but_not_the_application_secrets(tmp_path,
                                                                                       monkeypatch):
    """Behind a company proxy the first run can only download the models with these set."""
    from app import engines

    (tmp_path / "work").mkdir()
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs["env"])
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:8080")
    monkeypatch.setenv("https_proxy", "http://proxy.example.com:8080")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("HF_ENDPOINT", "https://mirror.example.com")
    monkeypatch.setenv("INV_STUDIO_VAULT_KEY", "not-for-the-reader")
    monkeypatch.setattr(engines.subprocess, "run", fake_run)
    with pytest.raises(ValueError):
        engines.local_read("paddleocr", tmp_path / "invoice-scan.png", tmp_path)
    for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "HF_ENDPOINT"):
        assert seen.get(name), f"the reader cannot reach the model hosts without {name}"
    assert "INV_STUDIO_VAULT_KEY" not in seen, "workers never receive the application's secrets"


def test_the_check_does_not_repeat_a_refused_download_just_to_report_it(monkeypatch, capsys):
    """The reader's exception class already says why; asking it again would download again."""
    from app import engines

    monkeypatch.setattr(engines, "capabilities", lambda: [
        {"id": engine, "installed": True, "models_blocked": False, "version": "2.133.0", "note": ""}
        for engine in ("invoice2data", "paddleocr", "docling")])
    monkeypatch.setattr(check_engines, "read",
                        lambda engine, data: (False, "This picture could not be read.", True,
                                              engines.DOWNLOAD_REFUSED))
    monkeypatch.setattr(check_engines, "direct_error",
                        lambda engine: pytest.fail("the refused download must not be repeated"))
    assert check_engines.main(["--any", "docling"]) == 1, "no scan reader is left working"
    printed = capsys.readouterr().out
    assert "NO MODELS" in printed and "refused its model download" in printed
    assert "could not be read" not in printed, "that hint names the document, not the real cause"


def test_each_disk_must_hold_every_install_folder_on_it(tmp_path, monkeypatch):
    """The check once measured only the .venv's disk; the cache on a full home disk then stopped the install."""
    other = tmp_path / "other"
    other.mkdir()
    devices = {str(tmp_path / "venv"): 1, str(other): 2}
    monkeypatch.setattr(install_ocr, "os", types.SimpleNamespace(
        stat=lambda path: types.SimpleNamespace(st_dev=devices[str(path)])))
    free = {str(tmp_path / "venv"): 10e9, str(other): 3.5e9}
    monkeypatch.setattr(install_ocr.shutil, "disk_usage", lambda path: types.SimpleNamespace(free=free[str(path)]))
    (tmp_path / "venv").mkdir()
    found = [("the .venv folder", tmp_path / "venv", 3, "move the inv-studio folder"),
             ("the download cache", other / "uv" / "not-made-yet", 3, "set UV_CACHE_DIR"),
             ("the Hugging Face model cache", other, 1, "set HF_HOME")]
    told = install_ocr.disk_problems(found)
    assert len(told) == 1, "the .venv's disk has room; only the other one is short"
    assert "Only 3.5 GB" in told[0] and "need about 4 GB" in told[0]
    assert "the download cache" in told[0] and "UV_CACHE_DIR" in told[0] and "HF_HOME" in told[0]
    assert ".venv" not in told[0]
    free[str(other)] = 4.5e9
    assert install_ocr.disk_problems(found) == []


def test_the_install_folders_include_the_temp_folder_and_both_model_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("PADDLE_PDX_CACHE_HOME", str(tmp_path / "pdx"))
    found = {what: (str(folder), move) for what, folder, _, move in install_ocr.places("pip")}
    assert {"the .venv folder", "the temporary folder"} <= set(found)
    assert found["the Hugging Face model cache"] == (str(tmp_path / "hf"), "set HF_HOME")
    assert found["the PaddleOCR models"] == (str(tmp_path / "pdx"), "set PADDLE_PDX_CACHE_HOME"), (
        "PaddleX keeps PaddleOCR's models there, not in the Hugging Face cache")
    monkeypatch.delenv("PADDLE_PDX_CACHE_HOME")
    paddlex = next(folder for what, folder, *_ in install_ocr.places("pip") if what == "the PaddleOCR models")
    assert paddlex == pathlib.Path.home() / ".paddlex"
