"""Independent pins and metadata-only authority checks."""

import hashlib
import json
from pathlib import Path

from trading_research.v2_protocol import load_protocol, verify_authorities, verify_bindings
from trading_runtime.config import SafetyError

PINS = {
    "base_protocol_hash": "0b14bae276520cc088440f87bf5865441e62c7cdb51504e5dde2e5123edc1c46",
    "clarification_001_hash": "210e9d8f10eb0cda1aba6f7381bebbc471665406689b61966a3edc1de8dafc53",
    "clarification_002_hash": "442004fb6866856495523bebe04f5b1d5289eddd0a08634c3859fb1fe88910b2",
    "clarification_003_hash": "78098f2451c7fb4d76db30a6c0b169c8e8659f3468bef65c2eebc5ec7b632847",
    "clarification_004_hash": "e75d394351ff7125d7b90115d551501907d6a727174f18202089061d97561b9f",
    "latest_effective_execution_spec_hash": "d6e07222edb37f95e310b062d0cf980e7d550328b3c9b05c884487c1c8d840ab",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require_pins(value):
    if any(value.get(k) != v for k, v in PINS.items()):
        raise SafetyError("D1_BINDING_MISMATCH")


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SafetyError("D1_DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)


class Spec:
    """Verified JSON stored as bytes; callers cannot mutate the authority."""

    def __init__(self, directory):
        directory = Path(directory)
        base = load_protocol(directory / "protocol-v2.json", expected_hash=PINS["base_protocol_hash"])
        clarifications = []
        for n in range(1, 5):
            value = read_json(directory / f"PROTOCOL_CLARIFICATION_D1_{n:03}.json")
            field = "clarification_hash" if n == 1 else f"clarification_{n:03}_hash"
            recorded = value.pop(field, None)
            if recorded != PINS[f"clarification_{n:03}_hash"] or digest(value) != recorded:
                raise SafetyError("D1_BINDING_MISMATCH")
            clarifications.append(value)
        binding = {k: v for k, v in PINS.items() if k != "latest_effective_execution_spec_hash"}
        if digest(binding) != PINS["latest_effective_execution_spec_hash"]:
            raise SafetyError("D1_BINDING_MISMATCH")
        self._payload = canonical({"base": base, "clarifications": clarifications})

    @property
    def base(self):
        return json.loads(self._payload)["base"]

    def clarification(self, number):
        return json.loads(self._payload)["clarifications"][number - 1]

    def verify_metadata(self, project, view, parent, checksums):
        verify_authorities(self.base, project)
        verify_bindings(self.base, view, parent, checksums)
        return "PASS"

    def config(self):
        return {**PINS, "phase": "3V2-D1", "commands": ["validate", "conformance"],
                "data_mode": "SYNTHETIC_ONLY", "data_bindings": self.base["bindings"]}
