from pathlib import Path

import pytest

from v2.conftest import no_network as no_network, spec as spec, spec_dir as spec_dir


@pytest.fixture(autouse=True)
def no_archive_reads(monkeypatch):
    original = Path.open

    def guarded(path, *args, **kwargs):
        if "aist-data" in str(path).lower():
            raise AssertionError("D1R_ARCHIVE_ACCESS_FORBIDDEN")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
