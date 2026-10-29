"""Add the scan readers, PaddleOCR and Docling, to a local install.

scripts/setup.sh --ocr and scripts\\setup.cmd --ocr run this with the .venv's
Python after the base install:

    .venv/bin/python scripts/install_ocr.py [--uv] [--dry-run]

It installs what the cloud image installs (Dockerfile), in the same order and
at the versions pinned in constraints.txt: PyTorch's CPU-only build, then the
docling and paddle extras, then the headless OpenCV build last. The readers
pull in three OpenCV packages that share one cv2 folder; installing the
headless build last leaves one consistent copy. It then runs
scripts/check_engines.py, which downloads the OCR models once and reads the
sample invoices with every reader it installed. One working scan reader is
enough: when a model download is refused (a company network often blocks the
model hosts) the install keeps the readers that do work and says so.

Which readers a computer gets (plan() below):
- Windows 10/11 64-bit, Linux x86_64, Apple silicon on macOS 14+: both.
- Apple silicon on macOS 11-13: PaddleOCR. Docling needs PyTorch, which needs
  macOS 14 on Apple silicon.
- Intel Macs on macOS 12+: PaddleOCR, on paddlepaddle 3.0.0, the last release
  with an Intel Mac build. Docling needs a PyTorch that has no Intel Mac build.
"""
import argparse
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
INTEL_MAC_PADDLE = "3.0.0"
VC_REDIST = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
# The longest file path inside the Windows wheels is 134 characters (modelscope,
# paddlepaddle). Windows refuses longer than 259 unless long paths are enabled.
LONGEST_WHEEL_PATH = 140
# What the install writes where, in GB, rounded up from Linux x86_64 at the constraints.txt pins:
# 2.5 in the .venv, 2.7 in the download cache, 0.5 in the Hugging Face cache (Docling's models, and
# PaddleOCR's when PaddleX fetches them from there) and 0.02 in PaddleX's own model folder, where
# PaddleOCR keeps its copy. These folders are often on different disks, so each disk is checked for
# what lands on it.
VENV_GB, CACHE_GB, TEMP_GB, HF_GB, PADDLEX_GB = 3, 3, 1, 1, 0.5
# Ready-made builds only: a source build needs compilers this computer may not have,
# and fails late with a compiler error. The exception is pure Python and has no wheel.
BINARY_ONLY = ["--only-binary", ":all:", "--no-binary", "antlr4-python3-runtime"]
NAMES = {"invoice2data": "invoice2data", "paddleocr": "PaddleOCR", "docling": "Docling"}


def normal(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(text):
    """{name: version} from the name==version lines of a constraints file."""
    found = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==(\S+)", line.split("#", 1)[0].strip())
        if match:
            found[normal(match.group(1))] = match.group(2)
    return found


def extras():
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["optional-dependencies"]


def refuse(message):
    return {"refuse": message}


def plan(system, machine, mac_version="", translated=False, python=(3, 12), bits=64):
    """The readers this computer gets and how to install them, or why it gets none."""
    machine = machine.lower()
    if tuple(python[:2]) != (3, 12):
        return refuse(f"The scan readers are installed and tested on Python 3.12; this .venv runs "
                      f"Python {python[0]}.{python[1]}. Install Python 3.12, delete the .venv folder "
                      "and run setup again.")
    both = {"readers": ["paddleocr", "docling"], "skipped": {}, "paddle": None,
            "headless_opencv": True, "drop": set()}
    if system == "Windows":
        if bits != 64 or machine not in ("amd64", "x86_64"):
            return refuse("The scan readers need 64-bit Windows on an Intel or AMD processor, and the "
                          "64-bit Python 3.12 from python.org.")
        return {**both, "torch": "cpu-index"}
    if system == "Linux":
        if machine not in ("x86_64", "amd64"):
            return refuse(f"On Linux the scan readers need an x86_64 processor; PaddlePaddle has no "
                          f"build for {machine}.")
        return {**both, "torch": "cpu-index"}
    if system != "Darwin":
        return refuse(f"There is no scan-reader install for {system}.")
    if translated:
        return refuse("This Python is the Intel build, running under Rosetta on an Apple silicon Mac. "
                      "Install the Apple silicon Python 3.12 (the python.org installer, or Homebrew in "
                      "/opt/homebrew), delete the .venv folder and run setup again.")
    try:
        major = int(mac_version.split(".")[0])
    except ValueError:
        major = 0
    # setup.sh installed the newest pypdfium2 and cryptography with a build for this
    # Mac; the cloud image's pins for them can be newer than older Macs support.
    paddle_only = {"readers": ["paddleocr"], "torch": None, "paddle": None, "headless_opencv": False,
                   "drop": {"cryptography", "pypdfium2"}}
    if machine == "arm64":
        if major >= 14:
            return {**both, "torch": "pypi", "drop": {"cryptography", "pypdfium2"}}
        if major >= 11:
            return {**paddle_only, "skipped": {
                "docling": "Docling needs PyTorch, which needs macOS 14 (Sonoma) or newer on Apple silicon."}}
        return refuse("Invoice Studio needs macOS 11 (Big Sur) or newer.")
    if machine == "x86_64":
        if major >= 12:
            return {**paddle_only, "paddle": INTEL_MAC_PADDLE,
                    "drop": paddle_only["drop"] | {"paddlepaddle"},
                    "skipped": {"docling": "Docling needs PyTorch, which no longer ships for Intel Macs."}}
        return refuse("On an Intel Mac the scan reader needs macOS 12 (Monterey) or newer. Use the cloud "
                      "version of Invoice Studio for scanned invoices.")
    return refuse(f"There is no scan-reader install for a Mac with a {machine} processor.")


def constraints_for(chosen, text):
    """constraints.txt without the pins this plan replaces or cannot use on this computer."""
    kept = []
    for line in text.splitlines():
        match = re.match(r"([A-Za-z0-9._-]+)==(\S+)", line.strip())
        name = normal(match.group(1)) if match else None
        if name in chosen["drop"]:
            continue
        if name in ("torch", "torchvision"):
            if chosen["torch"] is None:
                continue
            if chosen["torch"] == "pypi":
                # PyPI's macOS builds carry no "+cpu" label; they are CPU builds.
                line = f"{match.group(1)}=={match.group(2).split('+')[0]}"
        kept.append(line)
    return "\n".join(kept) + "\n"


def reader_requirements(chosen, optional):
    wanted = []
    if "paddleocr" in chosen["readers"]:
        for requirement in optional["paddle"]:
            if chosen["paddle"] and normal(re.split(r"[=<>!~;\[ ]", requirement)[0]) == "paddlepaddle":
                requirement = f"paddlepaddle=={chosen['paddle']}"
            wanted.append(requirement)
    if "docling" in chosen["readers"]:
        wanted += optional["docling"]
    return wanted


def commands(chosen, pinned, optional, installer, python, constraints):
    """The install steps of a plan, as (label, argument list) pairs."""
    if installer == "uv":
        install = ["uv", "pip", "install", "--python", python]
        reinstall, check = "--reinstall", ["uv", "pip", "check", "--python", python]
    else:
        install = [python, "-m", "pip", "install", "--disable-pip-version-check"]
        reinstall, check = "--force-reinstall", [python, "-m", "pip", "check"]
    common = [*install, *BINARY_ONLY, "--constraint", constraints]
    steps = []
    if chosen["torch"]:
        torch = [f"{name}=={pinned[name]}" for name in ("torch", "torchvision")]
        if chosen["torch"] == "cpu-index":
            steps.append(("Installing PyTorch (CPU-only build)", [*common, "--index-url", CPU_INDEX, *torch]))
        else:
            steps.append(("Installing PyTorch", [*common, *(r.split("+")[0] for r in torch)]))
    readers = " and ".join(NAMES[reader] for reader in chosen["readers"])
    steps.append((f"Installing {readers} (several minutes the first time)",
                  [*common, *reader_requirements(chosen, optional)]))
    if chosen["headless_opencv"]:
        steps.append(("Installing the headless OpenCV build last",
                      [*install, *BINARY_ONLY, reinstall, "--no-deps",
                       f"numpy=={pinned['numpy']}",
                       f"opencv-python-headless=={pinned['opencv-python-headless']}"]))
    steps.append(("Checking that the installed packages agree", check))
    return steps


def this_computer():
    system, machine = platform.system(), platform.machine()
    mac_version, translated = "", False
    if system == "Darwin":
        # platform.mac_ver() says "10.16" to a Python built with an old SDK; the kernel says the real one.
        found = subprocess.run(["sysctl", "-n", "kern.osproductversion"], capture_output=True, text=True)
        mac_version = found.stdout.strip() or platform.mac_ver()[0]
        found = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True)
        translated = found.stdout.strip() == "1"
    return system, machine, mac_version, translated


def missing_windows_runtime():
    import ctypes

    missing = []
    for library in ("msvcp140.dll", "vcruntime140_1.dll"):
        try:
            ctypes.WinDLL(library)
        except OSError:
            missing.append(library)
    return missing


def windows_long_paths():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def places(installer):
    """(what, folder, GB the install writes there, how to move it) for every folder the install fills."""
    cache = ["uv", "cache", "dir"] if installer == "uv" else [sys.executable, "-m", "pip", "cache", "dir"]
    cache = subprocess.run(cache, capture_output=True, text=True).stdout.strip()
    models = os.environ.get("HF_HOME") or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache",
                                               "huggingface")
    found = [("the .venv folder", sys.prefix, VENV_GB, "move the inv-studio folder"),
             ("the temporary folder", tempfile.gettempdir(), TEMP_GB, "set TMPDIR (Windows: TEMP)"),
             ("the Hugging Face model cache", models, HF_GB, "set HF_HOME"),
             ("the PaddleOCR models", os.environ.get("PADDLE_PDX_CACHE_HOME") or Path.home() / ".paddlex",
              PADDLEX_GB, "set PADDLE_PDX_CACHE_HOME")]
    if cache:  # empty when the pip cache is turned off
        found.append(("the download cache", cache, CACHE_GB, f"set {'UV' if installer == 'uv' else 'PIP'}_CACHE_DIR"))
    return found


def disk_problems(found):
    """One message per disk with less free space than the folders on it need together."""
    disks = {}
    for what, folder, gb, move in found:
        folder = Path(folder).absolute()
        while not folder.exists():
            folder = folder.parent
        disks.setdefault(os.stat(folder).st_dev, []).append((what, folder, gb, move))
    messages = []
    for on_disk in disks.values():
        need, free = sum(gb for _, _, gb, _ in on_disk), shutil.disk_usage(on_disk[0][1]).free / 1e9
        if free < need:
            names = "; ".join(f"{what} ({folder})" for what, folder, _, _ in on_disk)
            moves = "; ".join(f"{move} for {what}" for what, _, _, move in on_disk)
            messages.append(f"Only {free:.1f} GB is free on the disk that holds {names}; the scan readers "
                            f"need about {need:g} GB there. Free some space, or move them to a disk with "
                            f"room ({moves}), and run setup again.")
    return messages


def problems(system, installer):
    """What would stop the install part-way, found before it starts."""
    found = disk_problems(places(installer))
    if system == "Windows":
        missing = missing_windows_runtime()
        if missing:
            found.append(f"The Microsoft Visual C++ Redistributable (x64) is not installed "
                         f"({', '.join(missing)} missing); PyTorch and PaddlePaddle need it. Install it from "
                         f"{VC_REDIST}, then run setup again.")
        site = sysconfig.get_paths()["purelib"]
        if not windows_long_paths() and len(site) + LONGEST_WHEEL_PATH > 259:
            found.append(f"The folder path is too long for Windows ({site}). Move the inv-studio folder to a "
                         "short path such as C:\\inv-studio, delete its .venv folder and run setup again, or "
                         "turn on Windows long paths.")
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description="Add PaddleOCR and Docling to a local install.")
    parser.add_argument("--uv", action="store_true", help="install with uv instead of pip")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and commands; install nothing")
    args = parser.parse_args(argv)
    system, machine, mac_version, translated = this_computer()
    chosen = plan(system, machine, mac_version, translated, sys.version_info[:2], struct.calcsize("P") * 8)
    if "refuse" in chosen:
        print("Scan readers not installed: " + chosen["refuse"], file=sys.stderr)
        return 1
    print("==> Scan readers for this computer: " + ", ".join(NAMES[r] for r in chosen["readers"]))
    for reader, reason in chosen["skipped"].items():
        print(f"    {NAMES[reader]} is not available here. {reason}")
    found = [] if args.dry_run else problems(system, "uv" if args.uv else "pip")
    for problem in found:
        print("Scan readers not installed: " + problem, file=sys.stderr)
    if found:
        return 1
    text = (ROOT / "constraints.txt").read_text(encoding="utf-8")
    # --any: the scan readers read the same picture, so one of them working is a working install.
    check = [sys.executable, str(ROOT / "scripts" / "check_engines.py"), "invoice2data", "--any",
             *chosen["readers"]]
    with tempfile.TemporaryDirectory(prefix="inv-studio-ocr-") as work:
        constraints = Path(work) / "constraints.txt"
        constraints.write_text(constraints_for(chosen, text), encoding="utf-8")
        steps = commands(chosen, pins(text), extras(), "uv" if args.uv else "pip", sys.executable,
                         str(constraints))
        for label, command in steps:
            print(f"==> {label}", flush=True)
            if args.dry_run:
                print("    " + " ".join(command))
            elif subprocess.run(command, cwd=ROOT).returncode:
                print(f"Scan reader install stopped at: {label}. The messages above say why.",
                      file=sys.stderr)
                return 1
    print("==> Checking every reader on the sample invoices. The first run downloads the OCR models "
          "(about 600 MB), so it needs the internet and takes a few minutes. A reader whose download "
          "is refused is named below and can be added later, without reinstalling.", flush=True)
    if args.dry_run:
        print("    " + " ".join(check))
        return 0
    return subprocess.run(check, cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
