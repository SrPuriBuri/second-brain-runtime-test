"""Versioned immutable local snapshots. No upload or cache publication facility."""

import gzip
import json
import os
from pathlib import Path
import re
import tempfile

from trading_runtime.config import SafetyError
from .dataset_manifest import canonical, dataset_id, manifest, sha256

DOMAINS = {"market", "corporate_actions", "security_master", "news", "calendar"}


class DatasetCache:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, domain, identity, snapshot):
        if domain not in DOMAINS or not all(
            re.fullmatch(r"[a-f0-9]{64}", x) for x in (identity, snapshot)
        ):
            raise SafetyError("CACHE_PATH_INVALID")
        path = self.root / domain / identity / snapshot
        if not path.resolve().is_relative_to(self.root):
            raise SafetyError("CACHE_PATH_INVALID")
        return path

    def freeze(self, domain, config, payload, retention="EPHEMERAL_RESEARCH_ONLY"):
        meta = manifest(config, payload, retention)
        target = self._path(domain, meta["dataset_id"], meta["snapshot_id"])
        if target.exists():
            self.read(domain, meta["dataset_id"], meta["snapshot_id"])
            return json.loads((target / "manifest.json").read_text())
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".partial-", dir=target.parent))
        with (temporary / "payload.json.gz").open("wb") as handle:
            handle.write(gzip.compress(canonical(payload), mtime=0))
            handle.flush()
            os.fsync(handle.fileno())
        with (temporary / "manifest.json").open("wb") as handle:
            handle.write(canonical(meta))
            handle.flush()
            os.fsync(handle.fileno())
        # Directory rename publishes both files together. An interrupted directory
        # retains its .partial name and is never considered a valid snapshot.
        os.rename(temporary, target)
        return meta

    def read(self, domain, identity, snapshot):
        path = self._path(domain, identity, snapshot)
        try:
            meta = json.loads((path / "manifest.json").read_text())
            payload = json.loads(
                gzip.decompress((path / "payload.json.gz").read_bytes())
            )
            if (
                meta["state"] != "FROZEN"
                or meta["dataset_id"] != identity
                or meta["snapshot_id"] != snapshot
                or dataset_id(meta["selection"]) != identity
                or sha256(payload) != meta["content_sha256"]
                or sha256({"dataset_id": identity, "content_sha256": sha256(payload)})
                != snapshot
            ):
                raise ValueError()
            return payload
        except (OSError, ValueError, KeyError, EOFError):
            raise SafetyError("CACHE_INCOMPLETE_OR_CORRUPT") from None
