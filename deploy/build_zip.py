"""Build the AgentCore direct-code-deploy package.

Layout inside the zip (zip root == /var/task on the Runtime)::

    main.py                  entry point, puts ./src on sys.path and serves 8080
    src/biddesk/*.py         the package (config.ROOT resolves to the zip root)
    data/snapshot/*.json     the notices the demo reads
    data/bench/*.json        the tiering counts the cached triage serves
    data/attachments/<id>/extracted.json   requirement text only, never the raw PDFs
    data/geo, data/ledger, data/raw/throttle.json, gallery/, _runs/.keep
    <third-party packages>   linux/aarch64 wheels, pure or arm64 binaries

``.keep`` files exist for every directory ``biddesk.config`` creates at import
time, so the import never has to write to a read-only filesystem.

Run::  python deploy/build_zip.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from common import ROOT, say

STAGE = ROOT / "_runs" / "2026-09-13_phase6_deploy" / "stage"
ZIP_PATH = ROOT / "_runs" / "2026-09-13_phase6_deploy" / "biddesk_runtime.zip"

REQUIREMENTS = [
    "bedrock-agentcore==1.23.0",
    "strands-agents==1.55.1",
    "python-dotenv",
    "requests",
    "pydantic",
    "pypdf",
]

MAIN = '''"""AgentCore Runtime entry point. Serves 0.0.0.0:8080 with /invocations and /ping."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

from biddesk.service import app  # noqa: E402

app.run(port=int(os.environ.get("PORT", "8080")), host="0.0.0.0")
'''

SECRET_KEYS = ("SAM_API_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID")


def _copy_tree(src: Path, dst: Path, pattern: str = "*") -> int:
    n = 0
    for path in src.rglob(pattern):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        target = dst / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        n += 1
    return n


def stage() -> None:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    (STAGE / "main.py").write_text(MAIN, encoding="utf-8")

    pkg = STAGE / "src" / "biddesk"
    say("source files:", _copy_tree(ROOT / "src" / "biddesk", pkg, "*.py"))

    data = STAGE / "data"
    for sub in ("snapshot", "bench", "geo", "ledger"):
        src = ROOT / "data" / sub
        if src.exists():
            say(f"data/{sub}:", _copy_tree(src, data / sub))
    (data / "raw").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "data" / "raw" / "throttle.json", data / "raw" / "throttle.json")

    extracted = 0
    for path in (ROOT / "data" / "attachments").glob("*/extracted.json"):
        target = data / "attachments" / path.parent.name / "extracted.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        extracted += 1
    say("extracted.json files:", extracted)

    say("gallery files:", _copy_tree(ROOT / "gallery", STAGE / "gallery"))

    # every directory biddesk.config mkdirs at import time must already exist
    for rel in ("data/raw", "data/snapshot", "data/attachments", "gallery", "_runs"):
        d = STAGE / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / ".keep").write_text("", encoding="utf-8")


def vendor() -> None:
    """Download linux/aarch64 wheels into the zip root."""
    # uv resolves environment markers for the TARGET platform; pip resolves them for
    # the build machine, which drags Windows-only dependencies into the resolution.
    cmd = [sys.executable, "-m", "uv", "pip", "install",
           "--python-platform", "aarch64-manylinux2014", "--python-version", "3.13",
           "--only-binary=:all:", "--target", str(STAGE), *REQUIREMENTS]
    say("$", " ".join(cmd[2:]))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    for junk in list(STAGE.rglob("__pycache__")):
        shutil.rmtree(junk, ignore_errors=True)


def secret_gate() -> None:
    """Fail the build if any credential value appears anywhere in the staged tree."""
    values = [v for k, v in os.environ.items() if k in SECRET_KEYS and v]
    if not values:
        raise SystemExit("no credentials loaded; cannot run the secret gate")
    hits = 0
    for path in STAGE.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(v in text for v in values):
            hits += 1
            say("SECRET IN", path.relative_to(STAGE))
    if hits:
        raise SystemExit(f"secret gate failed: {hits} staged files contain a credential")
    say("secret gate: 0 staged files contain a credential value")
    if (STAGE / ".env").exists():
        raise SystemExit(".env must never be staged")


def write_zip() -> Path:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    count = 0
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(STAGE.rglob("*")):
            if path.is_dir():
                continue
            arc = str(path.relative_to(STAGE)).replace("\\", "/")
            info = zipfile.ZipInfo(arc)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.date_time = (2026, 9, 13, 0, 0, 0)
            # AgentCore needs 644 on files; directories it creates get 755 itself
            info.external_attr = (0o755 if path.suffix in (".so", ".sh") or os.access(path, os.X_OK) and path.suffix == "" else 0o644) << 16
            z.writestr(info, path.read_bytes())
            count += 1
    say("zipped files:", count)
    say("zip size:", f"{ZIP_PATH.stat().st_size / 1e6:.1f} MB", ZIP_PATH.name)
    return ZIP_PATH


if __name__ == "__main__":
    stage()
    vendor()
    secret_gate()
    write_zip()
