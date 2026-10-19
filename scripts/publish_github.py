"""Create the public source repository and push without putting a token in Git."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def publish(token_file: Path, owner: str | None, name: str) -> str:
    if token_file.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("Token file must be private (chmod 600)")
    token = token_file.read_text().strip()
    if not token:
        raise ValueError("GitHub token file is empty")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT):
        raise ValueError("Commit all source changes before publishing")
    with httpx.Client(timeout=30, headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}) as client:
        profile = client.get("https://api.github.com/user")
        profile.raise_for_status()
        login = profile.json()["login"]
        if owner and login.casefold() != owner.casefold():
            raise ValueError("Token belongs to another GitHub owner")
        remote = f"https://github.com/{login}/{name}.git"
        current = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True)
        if current.returncode == 0 and current.stdout.strip() != remote:
            raise ValueError("Origin points to another repository")
        repo = client.get(f"https://api.github.com/repos/{login}/{name}")
        if repo.status_code == 404:
            repo = client.post("https://api.github.com/user/repos", json={"name": name, "private": False,
                "description": "Self-hosted daily news: DSH research, verified reports, a static website and notifications.",
                "auto_init": False})
        repo.raise_for_status()
        if repo.json()["private"]:
            raise ValueError("Existing repository is private; refusing to change its visibility implicitly")
    if current.returncode:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix="newspaper-git-auth-") as temp:
        helper = Path(temp) / "askpass.sh"
        helper.write_text("#!/bin/sh\ncase \"$1\" in\n*Username*) printf '%s' 'x-access-token';;\n*) cat " + shlex.quote(str(token_file.resolve())) + ";;\nesac\n")
        helper.chmod(0o700)
        result = subprocess.run(["git", "push", "-u", "origin", "main"], cwd=ROOT,
            env={**os.environ, "GIT_ASKPASS": str(helper), "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError("Public repository exists, but Git push failed; inspect access or branch protection")
    return repo.json()["html_url"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, default=Path("/etc/newspaper/github.token"))
    parser.add_argument("--owner")
    parser.add_argument("--name", default="Newspaper")
    args = parser.parse_args()
    try:
        print(json.dumps({"repository": publish(args.token_file, args.owner, args.name)}))
    except Exception as exc:
        # Exception strings from network libraries may contain sensitive input.
        print(json.dumps({"error_type": type(exc).__name__}))
        raise SystemExit(1)
