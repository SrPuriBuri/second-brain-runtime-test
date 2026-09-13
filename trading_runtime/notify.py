"""Always-available console summary. Optional provider failures are data."""

import json
import os
from pathlib import Path

import httpx

from .config import redact


def notify(summary, env=None, client=None):
    env = os.environ if env is None else env
    # Caller passes counts and stable codes only; public job summaries are public.
    message = redact(json.dumps(summary, default=str), env)
    print(message)
    failures = []
    if env.get("GITHUB_STEP_SUMMARY"):
        try:
            with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as file:
                file.write(message + "\n")
        except OSError:
            failures.append("JOB_SUMMARY_FAILED")
    client = client or httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
    targets = []
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        targets.append(
            (
                "TELEGRAM",
                f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage",
                {"chat_id": env["TELEGRAM_CHAT_ID"], "text": message},
            )
        )
    if env.get("DISCORD_WEBHOOK_URL"):
        targets.append(
            ("DISCORD", env["DISCORD_WEBHOOK_URL"], {"content": message[:1900]})
        )
    for name, url, payload in targets:
        try:
            response = client.post(url, json=payload)
            response.raise_for_status()
        except Exception:
            failures.append(name + "_NOTIFICATION_FAILED")
    return failures
