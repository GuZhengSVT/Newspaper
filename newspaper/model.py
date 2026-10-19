from __future__ import annotations

import json
import os
import time

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
        self.usage = []

    def request(self, prompt: str, payload: dict) -> dict:
        key = os.environ.get(self.cfg.get("api_key_env", "D1API_API_KEY"))
        if not key:
            raise ValueError("Model credentials are not configured")
        started = time.monotonic()
        policy = ROOT / "prompts" / "editorial.md"
        system = (policy.read_text() + "\n" if policy.exists() else "") + (ROOT / "prompts" / f"{prompt}.md").read_text()
        encoded = json.dumps(payload, ensure_ascii=False)
        # Do not persist model request/response bodies or HTTP debug logs.
        with httpx.Client(timeout=self.cfg.get("timeout_seconds", 180)) as client:
            for attempt in range(3):
                try:
                    response = client.post(
                        self.cfg["base_url"].rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={
                            "model": self.cfg["id"],
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": encoded},
                            ],
                            "max_tokens": self.cfg.get("max_tokens", 12000),
                        },
                    )
                except httpx.TransportError:
                    if attempt == 2:
                        self.usage.append({"stage": prompt, "http_attempts": 3, "error_type": "TransportError", "tokens": None})
                        raise
                    time.sleep((5, 15)[attempt])
                    continue
                if response.status_code not in {429, 502, 503, 504} or attempt == 2:
                    break
                time.sleep((5, 15)[attempt])
            if response.is_error:
                self.usage.append({"stage": prompt, "http_attempts": attempt + 1,
                                   "status_code": response.status_code, "tokens": None})
            response.raise_for_status()
            data = response.json()
        self.usage.append({"stage": prompt, "input_chars": len(system) + len(encoded),
                           "duration_seconds": round(time.monotonic()-started, 2), "http_attempts": attempt + 1,
                           "tokens": data.get("usage")})
        message = data["choices"][0]
        if message.get("finish_reason") == "length":
            raise ValueError("Model output was truncated")
        return parse_json(message["message"]["content"])
