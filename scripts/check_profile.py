"""Offline DSH composition check: no credentials, agent run or network calls."""
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from newspaper.config import ROOT, Settings
from newspaper.research import prepare_profile

with tempfile.TemporaryDirectory(prefix="newspaper-profile-") as temp:
    directory = Path(temp)
    values = tomllib.loads((ROOT / "config.example.toml").read_text())
    settings = Settings(values, directory / "data", directory / "web", "http://localhost")
    overlay, env = prepare_profile(directory, settings)
    result = subprocess.run(
        [str(ROOT / "node_modules/.bin/dsh"), "--profile", "newspaper", "--patch", str(overlay), "--dump-config"],
        cwd=directory, env=env, capture_output=True, text=True, timeout=60,
    )
    if result.returncode or not all(text in result.stdout for text in [values["model"]["id"], "dsh-free-search", "web-search-free"]):
        raise SystemExit("DSH profile composition failed; check pinned runtime/peer dependencies")
print("DSH profile composition passed (no API calls).")
