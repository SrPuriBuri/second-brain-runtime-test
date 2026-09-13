"""Environment configuration; there is no live mode or execution toggle."""

import os
import re
from dataclasses import dataclass, field

PAPER_URL = "https://paper-api.alpaca.markets"
SECRET_NAMES = (
    "ALPACA_PAPER_API_KEY",
    "ALPACA_PAPER_SECRET_KEY",
    "SECOND_BRAIN_PAT",
    "GEMINI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DISCORD_WEBHOOK_URL",
    "ALPACA_LIVE_API_KEY",
    "ALPACA_LIVE_SECRET_KEY",
)


class SafetyError(RuntimeError):
    """Only stable, non-sensitive reason codes cross an external boundary."""


def redact(text, env=None):
    env = os.environ if env is None else env
    text = str(text)
    for key in SECRET_NAMES:
        value = env.get(key)
        if value:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|secret|token)([\s\"':=]+)[^\s,\"}]+", r"\1\2[REDACTED]", text
    )
    return text


def verify_paper_url(url):
    if str(getattr(url, "value", url)) != PAPER_URL:
        raise SafetyError("NOT_PAPER")


@dataclass(frozen=True)
class Config:
    key: str = field(repr=False)
    secret: str = field(repr=False)
    pat: str = field(default="", repr=False)
    provider: str = "no_ai"
    model: str = ""

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        if any(
            env.get(k)
            for k in (
                "ALPACA_LIVE_API_KEY",
                "ALPACA_LIVE_SECRET_KEY",
                "ALPACA_ENDPOINT",
            )
        ):
            raise SafetyError("FORBIDDEN_BROKER_CONFIGURATION")
        if (
            env.get("PAPER", "true").lower() != "true"
            or env.get("LIVE", "false").lower() != "false"
        ):
            raise SafetyError("NOT_PAPER")
        if not env.get("ALPACA_PAPER_API_KEY") or not env.get(
            "ALPACA_PAPER_SECRET_KEY"
        ):
            raise SafetyError("MISSING_PAPER_CREDENTIALS")
        provider = env.get("SECOND_BRAIN_TRADING_AI_PROVIDER", "no_ai")
        model = env.get("SECOND_BRAIN_TRADING_MODEL", "")
        if provider not in {"no_ai", "gemini"} or (provider == "gemini" and not model):
            raise SafetyError("INVALID_AI_CONFIGURATION")
        return cls(
            env["ALPACA_PAPER_API_KEY"],
            env["ALPACA_PAPER_SECRET_KEY"],
            env.get("SECOND_BRAIN_PAT", ""),
            provider,
            model,
        )
