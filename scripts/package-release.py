"""Build a reproducible deployable source release from one committed tree."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def git(*args: str) -> bytes:
    return subprocess.check_output(("git", "-C", str(ROOT), *args))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Git tag, branch or commit")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    commit = git("rev-parse", "--verify", "--end-of-options", f"{args.ref}^{{commit}}").decode().strip()
    timestamp = int(git("show", "-s", "--format=%ct", commit))
    project_file = tomllib.loads(git("show", f"{commit}:pyproject.toml").decode())
    version = project_file["project"]["version"]
    prefix = f"newspaper-{version}-{commit}"
    manifest = (f"version = {json.dumps(version)}\ncommit = {json.dumps(commit)}\n").encode()

    source = io.BytesIO(git("archive", "--format=tar", f"--prefix={prefix}/", commit))
    archive = io.BytesIO()
    with tarfile.open(fileobj=source, mode="r:") as contents:
        with gzip.GzipFile(fileobj=archive, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w:") as release:
                for member in contents:
                    release.addfile(member, contents.extractfile(member) if member.isfile() else None)
                entry = tarfile.TarInfo(f"{prefix}/release.toml")
                entry.size = len(manifest)
                entry.mode = 0o644
                entry.mtime = timestamp
                release.addfile(entry, io.BytesIO(manifest))

    payload = archive.getvalue()
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / f"{prefix}.tar.gz"
    checksum = target.with_name(target.name + ".sha256")
    if target.exists() or checksum.exists():
        parser.error("release archive or checksum already exists")
    with target.open("xb") as stream:
        stream.write(payload)
    with checksum.open("x", encoding="utf-8") as stream:
        stream.write(f"{hashlib.sha256(payload).hexdigest()}  {target.name}\n")
    print(target)


if __name__ == "__main__":
    main()
