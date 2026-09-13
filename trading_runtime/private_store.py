"""Restricted GitHub Contents API persistence; no private checkout at runtime."""

import base64
import json
import re
from dataclasses import dataclass

import httpx

from .config import SafetyError
from .models import KillSwitch, Policy, Strategy

ROOT = "projects/ai-stock-trader/"
API = "https://api.github.com/repos/SrPuriBuri/second-brain/contents/"
PROJECTIONS = {
    "state/current-state.json",
    "state/ownership-ledger.json",
    "state/readiness.json",
}
DATA_AREAS = {"journal", "progress", "handoffs", "evidence", "watchlists", "rejections"}


class Conflict(SafetyError):
    pass


@dataclass(frozen=True)
class File:
    text: str
    sha: str

    def json(self):
        return json.loads(self.text)


def validate_path(path):
    if not isinstance(path, str) or not path.startswith(ROOT):
        raise SafetyError("PRIVATE_PATH_FORBIDDEN")
    parts = path.split("/")
    if any(p in {"", ".", ".."} for p in parts) or not re.fullmatch(
        r"[A-Za-z0-9_./-]+", path
    ):
        raise SafetyError("PRIVATE_PATH_FORBIDDEN")
    return path[len(ROOT) :]


def authority_json(text):
    blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.S)
    if len(blocks) != 1:
        raise SafetyError("INVALID_AUTHORITY_DOCUMENT")
    try:
        return json.loads(blocks[0])
    except ValueError:
        raise SafetyError("INVALID_AUTHORITY_DOCUMENT") from None


class PrivateRepoStore:
    def __init__(self, token, client=None):
        if not token:
            raise SafetyError("MISSING_PRIVATE_REPO_CREDENTIAL")
        self._client = client or httpx.Client(
            timeout=20, follow_redirects=False, trust_env=False
        )
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method, path, **kwargs):
        validate_path(path)
        try:
            result = self._client.request(
                method, API + path, headers=self._headers, **kwargs
            )
        except Exception:
            raise SafetyError("PRIVATE_STORE_UNAVAILABLE") from None
        if result.status_code in {409, 422}:
            raise Conflict("PRIVATE_WRITE_CONFLICT")
        if result.status_code not in {200, 201, 404}:
            raise SafetyError("PRIVATE_STORE_HTTP_ERROR")
        return result

    def read_file(self, path):
        response = self._request("GET", path)
        if response.status_code == 404:
            return None
        try:
            obj = response.json()
            if obj.get("type") != "file":
                raise ValueError
            return File(base64.b64decode(obj["content"]).decode("utf-8"), obj["sha"])
        except Exception:
            raise SafetyError("PRIVATE_FILE_INVALID") from None

    def required(self, relative):
        result = self.read_file(ROOT + relative)
        if result is None:
            raise SafetyError("PRIVATE_REQUIRED_FILE_MISSING")
        return result

    def _write(self, path, data, sha=None):
        payload = {
            "message": "ai-stock-trader: persist operational evidence [skip ci]",
            "content": base64.b64encode(
                json.dumps(
                    data, sort_keys=True, allow_nan=False, indent=2, default=str
                ).encode()
            ).decode(),
        }
        if sha:
            payload["sha"] = sha
        response = self._request("PUT", path, json=payload)
        if response.status_code not in {200, 201}:
            raise SafetyError("PRIVATE_WRITE_FAILED")
        return response.json()["content"]["sha"]

    def create_append_only_record(self, path, data):
        relative = validate_path(path)
        parts = relative.split("/")
        if (
            len(parts) < 3
            or parts[0] != "data"
            or parts[1] not in DATA_AREAS
            or not path.endswith(".json")
        ):
            raise SafetyError("APPEND_PATH_FORBIDDEN")
        if self.read_file(path) is not None:
            raise Conflict("APPEND_RECORD_EXISTS")
        # Omitting SHA makes concurrent creates fail atomically at GitHub.
        return self._write(path, data)

    def update_json_projection(self, path, data, expected_sha):
        if validate_path(path) not in PROJECTIONS or not expected_sha:
            raise SafetyError("PROJECTION_PATH_FORBIDDEN")
        return self._write(path, data, expected_sha)

    def merge_projection(self, relative, transform, attempts=3):
        """Refetch/reconcile/retry; transform must re-evaluate latest state, never replay stale data."""
        for _ in range(attempts):
            current = self.required(relative)
            proposed = transform(current.json())
            try:
                return self.update_json_projection(
                    ROOT + relative, proposed, current.sha
                )
            except Conflict:
                continue
        raise Conflict("PROJECTION_RETRY_EXHAUSTED")

    def read_strategy(self):
        return Strategy.model_validate(
            authority_json(self.required("STRATEGY.md").text)
        )

    def read_policy(self):
        return Policy.model_validate(authority_json(self.required("POLICY.md").text))

    def read_kill_switch(self):
        return KillSwitch.model_validate(self.required("state/kill-switch.json").json())

    def read_current_state(self):
        return self.required("state/current-state.json").json()
