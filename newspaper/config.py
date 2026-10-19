from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Shanghai")


def load_credentials(path: Path) -> None:
    """Read literal KEY=value pairs; never execute shell expressions."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.replace("_", "").isalnum():
            raise ValueError("Invalid credentials file entry")
        parsed = shlex.split(value, comments=True)
        os.environ.setdefault(key, " ".join(parsed))


@dataclass(frozen=True)
class Settings:
    values: dict
    data: Path
    web: Path
    base_url: str

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        config_path = Path(path or os.environ.get("NEWSPAPER_CONFIG", "/etc/newspaper/config.toml"))
        values = tomllib.loads(config_path.read_text())
        load_credentials(Path(os.environ.get("NEWSPAPER_CREDENTIALS", "/etc/newspaper/credentials.env")))
        data = Path(os.environ.get("NEWSPAPER_DATA", values.get("data_dir", "/var/lib/newspaper"))).resolve()
        web = Path(os.environ.get("NEWSPAPER_WEB", values.get("web_dir", "/srv/newspaper"))).resolve()
        if data == web or data in web.parents or web in data.parents:
            raise ValueError("News data and published website must be separate directories")
        url = values.get("base_url", "http://localhost").rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        if values.get("retention_days", 30) != 30:
            raise ValueError("This project enforces a 30-day maximum retention")
        return cls(values, data, web, url)

    @property
    def sections(self) -> list[dict]:
        sections = [s for s in self.values.get("sections", []) if s.get("enabled", True)]
        ids = [s["id"] for s in sections]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate section id")
        return sections

    def prepare(self) -> None:
        for path in [self.data, self.data / "editions", self.data / "tmp", self.web, self.web / "releases"]:
            path.mkdir(parents=True, exist_ok=True)
