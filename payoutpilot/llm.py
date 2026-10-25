"""OpenAI-compatible chat client for any provider (Gemini, Groq, Ollama) plus a deterministic mock."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import httpx

PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-2.5-flash"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "ollama": ("http://localhost:11434/v1", "", "qwen2.5:3b"),
}


@dataclass
class ChatClient:
    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 120.0
    transport: httpx.BaseTransport | None = None

    def complete(self, system: str, user: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        with httpx.Client(timeout=self.timeout, transport=self.transport) as c:
            r = c.post(f"{self.base_url.rstrip('/')}/chat/completions", json=body, headers=headers)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""


class MockChat:
    """Keyword-overlap judge: lets the whole pipeline run offline and keeps tests deterministic."""

    model = "mock"

    def complete(self, system: str, user: str) -> str:
        crits = re.findall(r"^- (.+?) \(weight", user, re.M)
        brief = set(re.findall(r"[a-z]{4,}", user.split("SUBMISSION", 1)[0].lower()))
        sub = user.split("SUBMISSION", 1)[-1].lower()
        words = set(re.findall(r"[a-z]{4,}", sub))
        overlap = len(brief & words)
        base = min(10, 2 + overlap // 3 + min(len(sub) // 400, 3))
        if "reviewer" in system.lower():
            return json.dumps({"agrees": True, "notes": ["mock reviewer: scores consistent with text length/overlap"]})
        return json.dumps({"scores": {c: base for c in crits},
                           "reasons": [f"{overlap} brief keywords matched", f"{len(sub)} chars of content"]})


def client_from_env() -> ChatClient | MockChat:
    provider = os.environ.get("LLM_PROVIDER", "").lower()
    if not provider:  # auto: first free key found, else the deterministic offline judge
        provider = "gemini" if os.environ.get("GEMINI_API_KEY") else "groq" if os.environ.get("GROQ_API_KEY") else "mock"
    if provider == "mock":
        return MockChat()
    if provider in PROVIDERS:
        base, key_env, model = PROVIDERS[provider]
        key = os.environ.get(key_env, "") if key_env else ""
        if key_env and not key:
            raise RuntimeError(f"{key_env} must be set for LLM_PROVIDER={provider}")
        return ChatClient(os.environ.get("LLM_BASE_URL", base), os.environ.get("LLM_MODEL", model), key)
    # Any other OpenAI-compatible endpoint
    return ChatClient(os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ.get("LLM_API_KEY", ""))


def extract_json(text: str) -> dict:
    """Parse the first JSON object in a model reply (handles ```json fences and chatter)."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError(f"no JSON object in model reply: {text[:200]!r}")
