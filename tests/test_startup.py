import json, logging, sys
from types import SimpleNamespace

from livehub import paths, startup


def test_porta_livre_usa_preferida():
    assert startup.escolher_porta(8782, livre=lambda p: True) == 8782


def test_primeira_vez_porta_ocupada_escolhe_alternativa():
    ocupadas = {8782, 8783, 8784}
    assert startup.escolher_porta(8782, livre=lambda p: p not in ocupadas, primeira_vez=True) == 8785


def test_primeira_vez_sem_alternativa():
    assert startup.escolher_porta(8782, livre=lambda p: False, primeira_vez=True) is None


def test_execucao_posterior_porta_ocupada_nao_troca():
    assert startup.escolher_porta(8782, livre=lambda p: p != 8782) is None


def test_porta_configurada(data_dir):
    assert startup.porta_configurada() == 8782
    (data_dir / "config.json").write_text(json.dumps({"web_port": 9000}), encoding="utf-8")
    assert startup.porta_configurada() == 9000


class FakeKernel:
    def __init__(self, erro):
        self.erro = erro
        self.CreateMutexW = lambda *a: 1234
        self.GetLastError = lambda: self.erro


def test_singleton_primeira_instancia():
    assert startup.acquire_singleton(FakeKernel(0)) == 1234


def test_singleton_ja_existe():
    assert startup.acquire_singleton(FakeKernel(183)) is None


class FakeKey:
    def __init__(self, store): self.store = store
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeWinreg:
    HKEY_CURRENT_USER = 1; KEY_READ = 2; KEY_SET_VALUE = 3; REG_SZ = 4

    def __init__(self): self.store = {}
    def OpenKey(self, *a): return FakeKey(self.store)
    def CreateKeyEx(self, *a): return FakeKey(self.store)
    def QueryValueEx(self, k, name):
        if name not in self.store: raise FileNotFoundError(name)
        return self.store[name], 4
    def SetValueEx(self, k, name, _r, _t, val): self.store[name] = val
    def DeleteValue(self, k, name):
        if name not in self.store: raise FileNotFoundError(name)
        del self.store[name]


def test_autostart_ciclo():
    w = FakeWinreg()
    assert not startup.autostart_enabled(w)
    startup.set_autostart(True, r"C:\Pasta X\LiveHub.exe", w)
    assert w.store["LiveHub"] == r'"C:\Pasta X\LiveHub.exe"'
    assert startup.autostart_enabled(w)
    startup.set_autostart(False, winreg=w)
    assert not startup.autostart_enabled(w)
    startup.set_autostart(False, winreg=w)  # idempotente


def test_logging_sem_stderr(monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdout", None)
    startup.force_utf8()
    raiz = logging.getLogger()
    antigos = list(raiz.handlers)
    try:
        startup.setup_logging()
        assert len(raiz.handlers) == 1
        logging.getLogger("t").error("ação ok")
        for h in raiz.handlers:
            h.flush()
        assert "ação ok" in paths.log_file().read_text(encoding="utf-8")
    finally:
        for h in list(raiz.handlers):
            h.close(); raiz.removeHandler(h)
        for h in antigos:
            raiz.addHandler(h)
