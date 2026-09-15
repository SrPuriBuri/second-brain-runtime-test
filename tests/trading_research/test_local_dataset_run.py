"""No-network local prompt, credential persistence and authorization regressions."""

from copy import deepcopy
import getpass
import gzip
import json
import logging

import httpx
import pytest

from scripts import trading_dataset_local_run as launcher
from trading_research.archive import (
    Archive,
    PRIVATE_RESEARCH_SCOPE,
    USER_AUTHORIZED_PRIVATE_RESEARCH,
    filesystem_path,
    make_plan,
    require_retention,
)
from trading_research.dataset_manifest import sha256
from trading_research.downloader import Downloader
from trading_runtime.config import SafetyError

KEY = "FAKE_LOCAL_API_CREDENTIAL_1234"
SECRET = "FAKE_LOCAL_SECRET_CREDENTIAL_5678"


def authorized():
    return {
        "status": USER_AUTHORIZED_PRIVATE_RESEARCH,
        "authorization_scope": deepcopy(PRIVATE_RESEARCH_SCOPE),
        "authorized_by": "user",
        "authorized_at": "2026-09-15T00:00:00Z",
        "provider_permission_verified": False,
        "provider": "alpaca",
        "feed": "sip",
        "private_local_retention": True,
        "source_ref": "synthetic user instruction",
        "reviewed_at": "2026-09-15T00:00:00Z",
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project = tmp_path / "private-project"
    state = project / "state"
    state.mkdir(parents=True)
    (project / "STRATEGY.md").write_text('```json\n{"tradable": false}\n```')
    (state / "kill-switch.json").write_text('{"enabled": true}')
    (state / "readiness.json").write_text(
        '{"execution_enabled": false, "execution_ready": false}'
    )
    base = project / "research-v2/dataset-v2"
    base.mkdir(parents=True)
    plan = make_plan()
    plan["selection"].update(
        symbols=["SPY"], archive_start="2024-03-08", archive_end="2024-03-08"
    )
    plan["dataset_id"] = sha256(plan["selection"])
    (base / "ACQUISITION_PLAN_V2.json").write_text(json.dumps(plan))
    (base / "RETENTION_REVIEW_V2.json").write_text(json.dumps(authorized()))
    root = tmp_path / "raw"
    monkeypatch.setenv("AIST_DATA_ROOT", str(root))
    env = {"AIST_DATA_ROOT": str(root)}
    return project, base, root, plan, env


def assert_no_persisted_credentials(root):
    for path in filesystem_path(root).rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            if path.suffix == ".gz":
                content = gzip.decompress(content)
            assert KEY.encode() not in content
            assert SECRET.encode() not in content


@pytest.mark.parametrize("use_environment", [True, False])
def test_real_downloader_credentials_only_in_memory(
    setup, monkeypatch, capsys, use_environment
):
    project, base, root, plan, env = setup
    if use_environment:
        env.update(zip(launcher.KEY_NAMES, (KEY, SECRET)))
    prompts, calls = [], []
    responses = iter((KEY, SECRET))

    def prompt(label):
        assert not use_environment
        prompts.append(label)
        return next(responses)

    def handler(request):
        assert request.method == "GET"
        assert request.headers["APCA-API-KEY-ID"] == KEY
        assert request.headers["APCA-API-SECRET-KEY"] == SECRET
        calls.append(request.url.path)
        if request.url.path.endswith("calendar"):
            body = [dict(date="2024-03-08", open="09:30", close="16:00")]
        elif request.url.path.endswith("assets"):
            body = [
                dict(symbol="SPY", id="synthetic-id", status="active", tradable=True)
            ]
        elif request.url.path.endswith("corporate-actions"):
            body = {"corporate_actions": {}}
        else:
            body = {
                "bars": {
                    "SPY": [
                        dict(t="2024-03-08T14:30:00Z", o=100, h=101, l=99, c=100, v=10)
                    ]
                }
            }
        return httpx.Response(200, json=body)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    # Keep the provider's real pacing without any external HTTP.
    assert (
        launcher.main(
            ["--project", str(project)], env=env, prompt=prompt, client_factory=factory
        )
        == 0
    )
    assert len(calls) == 4
    assert len(prompts) == (0 if use_environment else 2)
    import os

    assert os.environ.get(launcher.KEY_NAMES[0]) != KEY
    assert os.environ.get(launcher.KEY_NAMES[1]) != SECRET
    archive = Archive(root, plan)
    assert archive.status()["completed_chunks"] == 1
    assert archive.status()["completed_request_pages"] == 4
    assert len(list(archive.root.rglob("manifest.json"))) == 5
    assert (archive.path / "checkpoint.json").exists()
    assert_no_persisted_credentials(root.parent)
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
    assert SECRET not in output.out + output.err


@pytest.mark.parametrize("exception", [RuntimeError, SafetyError])
def test_exception_and_logging_never_reveal_secrets(setup, capsys, exception):
    project, _, root, _, env = setup
    prompts = iter((KEY, SECRET))
    disk_log = root.parent / "test.log"
    handler = logging.FileHandler(disk_log)
    logger = logging.getLogger("fake_http_headers")
    logger.addHandler(handler)

    def bad_run(*args, **kwargs):
        print(KEY)
        print("Authorization: Bearer " + SECRET)
        logger.critical("API key: %s secret: %s", KEY, SECRET)
        raise exception(KEY + SECRET)

    try:
        assert (
            launcher.main(
                ["--project", str(project)],
                env=env,
                prompt=lambda _: next(prompts),
                run=bad_run,
            )
            == 1
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert_no_persisted_credentials(root.parent)
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err
    assert SECRET not in captured.out + captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "cancel", [KeyboardInterrupt, EOFError, getpass.GetPassWarning]
)
def test_prompt_cancellation_or_insecure_console_never_calls_provider(setup, cancel):
    project, _, root, _, env = setup

    def prompt(_):
        raise cancel()

    code = launcher.main(
        ["--project", str(project)],
        env=env,
        prompt=prompt,
        client_factory=lambda **_: pytest.fail("provider created"),
    )
    assert code == (1 if cancel is getpass.GetPassWarning else 130)
    assert_no_persisted_credentials(root.parent)


def test_empty_credentials_never_call_provider(setup):
    project, _, _, _, env = setup
    assert (
        launcher.main(
            ["--project", str(project)],
            env=env,
            prompt=lambda _: "",
            client_factory=lambda **_: pytest.fail("provider created"),
        )
        == 1
    )


def test_hidden_prompt_disallows_echo_fallback(monkeypatch):
    import warnings

    def fallback(_):
        warnings.warn("cannot disable echo", getpass.GetPassWarning)
        pytest.fail("insecure input must never be read")

    monkeypatch.setattr(getpass, "getpass", fallback)
    with pytest.raises(getpass.GetPassWarning):
        launcher.hidden_prompt("Hidden: ")


def test_reflected_credentials_rejected_before_checkpoint(setup, capsys):
    project, _, root, plan, env = setup
    prompts = iter((KEY, SECRET))

    def factory(**kwargs):
        return httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"echo": SECRET})
            ),
            **kwargs,
        )

    assert (
        launcher.main(
            ["--project", str(project)],
            env=env,
            prompt=lambda _: next(prompts),
            client_factory=factory,
        )
        == 1
    )
    assert Archive(root, plan).status()["completed_request_pages"] == 0
    assert_no_persisted_credentials(root.parent)
    assert SECRET not in capsys.readouterr().out


def test_unresolved_still_blocks_without_user_authorization(setup):
    project, base, _, _, env = setup
    review = authorized()
    review["status"] = "STORAGE_TERMS_UNRESOLVED"
    (base / "RETENTION_REVIEW_V2.json").write_text(json.dumps(review))
    assert (
        launcher.main(
            ["--project", str(project)],
            env=env,
            prompt=lambda _: pytest.fail("prompt before authorization"),
        )
        == 1
    )


@pytest.mark.parametrize("field", list(PRIVATE_RESEARCH_SCOPE))
def test_private_authorization_rejects_broader_scope(field):
    evidence = authorized()
    require_retention(evidence)
    evidence["authorization_scope"][field] = not evidence["authorization_scope"][field]
    with pytest.raises(SafetyError):
        require_retention(evidence)


def test_direct_archive_rejects_git_root_under_user_authorization(tmp_path):
    require_retention(authorized())
    (tmp_path / ".git").mkdir()
    with pytest.raises(SafetyError, match="INSIDE_GIT"):
        Archive(tmp_path / "raw", make_plan())


def test_user_authorization_does_not_bypass_oos_or_route_guards(setup):
    _, _, root, plan, _ = setup
    archive = Archive(root, plan)
    d = Downloader(archive, object(), authorized())
    with pytest.raises(SafetyError, match="WINDOW_FORBIDDEN"):
        d.page("bars", {"start": "2024-12-31", "end": "2025-01-01"})
    with pytest.raises(SafetyError, match="ROUTE_FORBIDDEN"):
        d.page("orders", {})
    assert archive.status()["OOS_price_requests"] == 0


def test_unsafe_execution_state_blocks_before_prompt(setup):
    project, _, _, _, env = setup
    (project / "state/kill-switch.json").write_text('{"enabled": false}')
    assert (
        launcher.main(
            ["--project", str(project)],
            env=env,
            prompt=lambda _: pytest.fail("prompt before safety"),
        )
        == 1
    )
