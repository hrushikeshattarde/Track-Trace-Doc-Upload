"""Load this project's own .env so every script (readiness, mail_survey, run, read_attachments) can run from
the project virtualenv without the payment-bot checkout's environment.

Rules:
- Only KEY=VALUE lines; '#' comments and blank lines ignored; surrounding quotes stripped.
- Never overrides a variable that is already set in the process environment.
- PAYBOT_GOOGLE_SA_FILE, if relative, is resolved against the project folder first, then the payment-bot folder.
- Values are never logged or printed by this module.
"""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PAYBOT_DIR = Path(r"C:\Users\hrushikesh.attarde_c\Desktop\payment-bot-intake-policies-and-hardening")


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def env_file_in_use() -> Path | None:
    """The .env this project will read: its own if present, else the payment-bot checkout's."""
    for p in (HERE / ".env", PAYBOT_DIR / ".env"):
        if p.exists():
            return p
    return None


def load_local_env() -> Path | None:
    """Populate os.environ from the .env in use (without overriding existing variables). Returns the file used."""
    path = env_file_in_use()
    if path is None:
        return None
    values = read_env_file(path)
    for key, value in values.items():
        if value == "":                      # a blank line in .env means "not set", never "set to empty"
            continue
        if key in MODEL_KEYS_AUTHORITATIVE:
            os.environ[key] = value          # the project's model configuration beats anything inherited from the shell
        else:
            os.environ.setdefault(key, value)
    if any(k in values and values[k] for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")):
        for k in MODEL_KEYS_AUTHORITATIVE:   # and clears leftovers the .env does not mention (a tool's own proxy settings)
            if not values.get(k) and k in os.environ:
                del os.environ[k]
    # The Anthropic SDK sends whatever ANTHROPIC_API_KEY holds, even an empty string, and a proxy then answers 401
    # "invalid x-api-key". With a bearer token configured, an empty api key must be absent, not blank.
    if os.environ.get("ANTHROPIC_AUTH_TOKEN") and os.environ.get("ANTHROPIC_API_KEY", None) == "":
        del os.environ["ANTHROPIC_API_KEY"]
    sa = os.environ.get("PAYBOT_GOOGLE_SA_FILE")
    if sa and not Path(sa).is_absolute():
        for base in (HERE, PAYBOT_DIR, path.parent):
            if (base / sa).exists():
                os.environ["PAYBOT_GOOGLE_SA_FILE"] = str(base / sa)
                break
    return path


def missing(keys: list[str]) -> list[str]:
    """Which of these variables are still unset or empty after load_local_env()."""
    return [k for k in keys if not os.environ.get(k)]


MODEL_KEYS_AUTHORITATIVE = ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
                            # Which AWS identity the service runs as is the project's decision, not the
                            # shell's. On 22 Sep 2026 a stale AWS_PROFILE inherited from the terminal
                            # silently won over the .env for a whole day of runs: everything worked, but
                            # as a different role than configured, and `aws sso login` on the profile the
                            # .env named refreshed a token nothing used.
                            "AWS_PROFILE", "AWS_REGION"]
TPRO_KEYS = ["PAYBOT_TP_BASE_URL", "PAYBOT_TP_USERNAME", "PAYBOT_TP_PASSWORD"]
GMAIL_KEYS = ["PAYBOT_GMAIL_USER", "PAYBOT_GOOGLE_SA_FILE"]
MODEL_KEYS_ANY = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY"]
