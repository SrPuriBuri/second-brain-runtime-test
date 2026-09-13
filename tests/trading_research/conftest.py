import socket
import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Research tests must never contact a real service")

    monkeypatch.setattr(socket.socket, "connect", blocked)
