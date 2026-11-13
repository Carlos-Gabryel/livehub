import socket
import pytest


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Cada teste usa o proprio LIVEHUB_DATA (nunca toca no %APPDATA% real)."""
    d = tmp_path / "data"
    monkeypatch.setenv("LIVEHUB_DATA", str(d))
    d.mkdir(parents=True, exist_ok=True)
    return d


def porta_livre():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


@pytest.fixture(autouse=True)
def _sem_update(monkeypatch):
    """O loop de fundo do atualizador nunca roda nos testes (eles chamam verificar() direto)."""
    monkeypatch.setenv("LIVEHUB_NO_UPDATE", "1")
