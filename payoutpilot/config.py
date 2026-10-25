"""Load KEY=VALUE env files (.env, .env.paypal) without overriding the real environment and without logging values."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_FILES = (".env", ".env.paypal")


def load_env_files(paths: tuple[str, ...] | list[str] = DEFAULT_FILES, root: str | Path = ".") -> list[str]:
    loaded: list[str] = []
    if os.environ.get("PAYOUTPILOT_NO_DOTENV") == "1":
        return loaded
    for name in paths:
        p = Path(root) / name
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.removeprefix("export ").partition("=")
            val = val.strip()
            if val[:1] in ("'", '"') and val[-1:] == val[:1] and len(val) > 1:
                val = val[1:-1]
            elif " #" in val:
                val = val.split(" #", 1)[0].strip()
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = val
        loaded.append(str(p))
    return loaded


def mask_email(email: str) -> str:
    """a***z@domain for evidence files and logs."""
    user, _, dom = (email or "").partition("@")
    if not dom:
        return "***"
    return (user[:2] + "***" + user[-1:] if len(user) > 3 else user[:1] + "***") + "@" + dom
