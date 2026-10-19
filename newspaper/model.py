from __future__ import annotations

import json
import os

import httpx

from .config import ROOT, Settings


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


class Model:
    def __init__(self, settings: Settings):
        self.cfg = settings.values["model"]

    def request(self, prompt: str, payload: dict) -> dict:
        key = os.environ.get(self.cfg.get("api_key_env", "D1API_API_KEY"))
        if not key:
            raise ValueError("Model credentials are not configured")
        # Do not persist model request/response bodies or HTTP debug logs.
        with httpx.Client(timeout=self.cfg.get("timeout_seconds", 180)) as client:
            response = client.post(
                self.cfg["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": self.cfg["id"],
                    "messages": [
                        {"role": "system", "content": (ROOT / "prompts" / f"{prompt}.md").read_text()},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "max_tokens": self.cfg.get("max_tokens", 12000),
                },
            )
            response.raise_for_status()
            data = response.json()
        message = data["choices"][0]
        if message.get("finish_reason") == "length":
            raise ValueError("Model output was truncated")
        return parse_json(message["message"]["content"])
