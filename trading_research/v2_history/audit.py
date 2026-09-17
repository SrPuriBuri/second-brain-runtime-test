"""D2 phase barrier, committed-code identity and access accounting."""

import gzip
import hashlib
import json
import os
import subprocess
import sys

from trading_research.archive import filesystem_path
from trading_research.v2.bindings import PINS, Spec, canonical, digest, read_json
from trading_research.v2.conformance import dependencies, implementation_files
from trading_research.v2.ledger import schedule
from trading_runtime.config import SafetyError
from .adapter import partitions

D1R_COMMIT = "5263dea5171c2ca2defe64cf0cef4085de509ead"
D1R_HASH = "b734d99d858689eba22c9ac33f06cae13657c37e4ae723610e78c79914c42a93"
CONFIG_HASH = "516938e9104d0ac4e606483dad97dab8fb69af179da199d1efed0a6cd23c41bb"
DEPENDENCY_HASH = "5a040539a8e85c4118ba39bc3291b4d0b1059ff9464795203a3cc65cadd6b6dc"


class Access:
    """Process audit hook: deny network and all archive price paths by default."""
    def __init__(self, root):
        self.root = self.key(root)
        self.allowed = set()
        self.phase_b = False
        self.counters = dict(historical_price_files_opened=0, historical_price_rows_read=0,
                             historical_price_rows_exposed_to_strategy=0, historical_signals=0,
                             historical_scored_trades=0, historical_R_calculations=0,
                             market_network_attempts=0, blocked_price_opens=0,
                             price_rows_2022_plus=0, price_rows_2025_plus=0)

    @staticmethod
    def key(path):
        return os.path.abspath(os.fsdecode(path)).replace("\\", "/").removeprefix("//?/").lower()

    def hook(self, event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            self.counters["market_network_attempts"] += 1
            raise SafetyError("D2_NETWORK_FORBIDDEN")
        if event == "open" and isinstance(args[0], (str, bytes)):
            key = self.key(args[0])
            if key.startswith(self.root + "/"):
                price = "/bars/" in key or "/provider_pages/" in key or "/objects/" in key
                if price:
                    if not self.phase_b or key not in self.allowed:
                        self.counters["blocked_price_opens"] += 1
                        raise SafetyError("D2_PRICE_ACCESS_FORBIDDEN")
                    self.counters["historical_price_files_opened"] += 1

    def install(self):
        sys.addaudithook(self.hook)

    def allow_price(self, path):
        if not self.phase_b:
            raise SafetyError("D2_ZERO_EXPOSURE_BARRIER")
        self.allowed.add(self.key(path))


def git(root, *args):
    return subprocess.check_output(["git", "-c", "safe.directory=*", "-C", str(root), *args], stderr=subprocess.DEVNULL)


def committed_identity(public, spec):
    core = implementation_files(public, committed=True)
    if digest(core) != D1R_HASH or digest(spec.config()) != CONFIG_HASH or digest(dependencies()) != DEPENDENCY_HASH:
        raise SafetyError("D2_BINDING_MISMATCH")
    names = sorted(p.relative_to(public).as_posix() for p in (public / "trading_research/v2_history").glob("*.py"))
    files = {}
    for name in names:
        raw = git(public, "show", "HEAD:"+name)
        if raw.replace(b"\r\n", b"\n") != (public/name).read_bytes().replace(b"\r\n", b"\n"):
            raise SafetyError("D2_UNCOMMITTED_ADAPTER")
        files[name] = hashlib.sha256(raw).hexdigest()
    binding = {**PINS, "D1R_implementation_hash": D1R_HASH, "D2_adapter_hash": digest(files),
               "execution_config_hash": CONFIG_HASH, "dependency_fingerprint": DEPENDENCY_HASH,
               "data_bindings": spec.base["bindings"]}
    return {**binding, "D2_development_runtime_hash": digest(binding), "adapter_files": files,
            "D2_adapter_commit": git(public, "rev-parse", "HEAD").decode().strip()}


def metadata(project, root):
    spec = Spec(project / "research-v2/protocol-v2")
    b = spec.base["bindings"]
    parent = filesystem_path(root / "ai-stock-trader/datasets" / b["parent_dataset_id"] / b["parent_snapshot_id"])
    view = read_json(filesystem_path(root / "ai-stock-trader/research-views" / b["child_view_id"] / "view.json"))
    spec.verify_metadata(project, view, read_json(parent / "manifest.json"), read_json(parent / "checksums.json"))
    checksums = json.loads((parent / "checksums.json").read_bytes())
    def checked(name, compressed=False):
        raw = (parent/name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != checksums[name]:
            raise SafetyError("D2_METADATA_HASH")
        return [json.loads(line) for line in gzip.decompress(raw).splitlines()] if compressed else json.loads(raw)
    plan = checked("provenance/plan.json")
    checkpoint = checked("provenance/checkpoint.json")
    calendar = checked("calendar/sessions.jsonl.gz", True)
    items = partitions(plan, checksums, checkpoint, calendar, view["mask"]["universe"], *spec.base["splits"]["development"])
    return spec, parent, items, calendar, view["mask"]


def verify_barrier(project, public, pre_commit, manifest):
    private = project.parents[1]
    name = "projects/ai-stock-trader/research-v2/development-v2/D2_PRE_DEVELOPMENT_MANIFEST_V2.json"
    blob = git(private, "show", pre_commit+":"+name)
    if blob.replace(b"\r\n", b"\n") != (private/name).read_bytes().replace(b"\r\n", b"\n") or json.loads(blob) != manifest:
        raise SafetyError("D2_PREFLIGHT_MANIFEST_MISMATCH")
    # The operator verifies ls-remote after push; the runner verifies the exact
    # corresponding local remote-tracking commit without a network dependency.
    if git(private, "rev-parse", "origin/main").decode().strip() != pre_commit:
        raise SafetyError("D2_MANIFEST_NOT_REMOTE_TRACKED")
    spec = Spec(project / "research-v2/protocol-v2")
    identity = committed_identity(public, spec)
    if any(manifest.get(k) != v for k, v in identity.items()):
        raise SafetyError("D2_RUNTIME_MISMATCH")
    if manifest.get("zero_exposure") != dict.fromkeys(("historical_price_files_opened", "historical_price_rows_read",
            "historical_price_rows_exposed_to_strategy", "historical_signals", "historical_scored_trades",
            "historical_R_calculations", "development_evaluations_started", "development_trials_consumed"), 0):
        raise SafetyError("D2_MANIFEST_NOT_ZERO_EXPOSURE")
    if manifest.get("development_schedule_hash") != digest([s for s in schedule(spec) if s[0] == "development"]):
        raise SafetyError("D2_SCHEDULE_BINDING")
    return identity


def immutable_json(path, value):
    data = canonical(value) + b"\n"
    if path.exists():
        if path.read_bytes() != data:
            raise SafetyError("D2_IMMUTABLE_OUTPUT_CONFLICT")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.rename(path)
