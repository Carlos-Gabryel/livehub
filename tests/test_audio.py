import numpy as np
import pytest

from livehub import paths
from livehub.resgates import audio, sons


class FakeSD:
    class WasapiSettings:
        def __init__(self, auto_convert=False):
            self.auto_convert = auto_convert

    def __init__(self):
        self.hostapis = [{"name": "MME"}, {"name": "Windows WASAPI"}]
        self.devices = [
            {"name": "Mic", "hostapi": 1, "max_output_channels": 0},
            {"name": "Speakers (MME)", "hostapi": 0, "max_output_channels": 2},
            {"name": "Speakers", "hostapi": 1, "max_output_channels": 2},
            {"name": "Meu Fone Virtual", "hostapi": 1, "max_output_channels": 2},
        ]
        self.plays, self.stops = [], 0

    def query_hostapis(self):
        return self.hostapis

    def query_devices(self, i=None):
        return self.devices if i is None else self.devices[i]

    def play(self, dados, taxa, **kw):
        self.plays.append((dados, taxa, kw))

    def wait(self):
        pass

    def stop(self):
        self.stops += 1


class FakeSF:
    @staticmethod
    def read(caminho, dtype="float32", always_2d=True):
        return np.ones((10, 1), dtype="float32"), 44100


@pytest.fixture
def fsd(monkeypatch):
    f = FakeSD()
    monkeypatch.setattr(audio, "sd", f)
    monkeypatch.setattr(audio, "sf", FakeSF)
    return f


def test_listar_saidas_so_wasapi(fsd):
    assert audio.listar_saidas() == ["Speakers", "Meu Fone Virtual"]


def test_dispositivo_por_nome(fsd):
    audio.tocar("x.wav", 0.5, "meu fone")
    dados, taxa, kw = fsd.plays[0]
    assert kw["device"] == 3 and kw["extra_settings"].auto_convert
    assert dados.shape == (10, 2) and float(dados[0, 0]) == 0.5  # mono -> estereo, volume aplicado


def test_vazio_usa_padrao(fsd):
    audio.tocar("x.wav", 1.0, "")
    assert fsd.plays[0][2] == {}


def test_dispositivo_inexistente_cai_no_padrao(fsd, caplog):
    with caplog.at_level("WARNING", logger="audio"):
        audio.tocar("x.wav", 1.0, "Nao Existe")
    assert fsd.plays[0][2] == {}
    assert "Nao Existe" in caplog.text


def test_parar(fsd):
    audio.parar()
    assert fsd.stops == 1


def test_sons_confinados_em_data_dir():
    (paths.sons_dir() / "sub").mkdir()
    (paths.sons_dir() / "sub" / "a.mp3").write_bytes(b"x")
    (paths.sons_dir() / "nota.txt").write_text("x")
    assert sons.listar_sons() == [str(__import__("pathlib").Path("sub") / "a.mp3")]
    assert sons.resolve_som("sub/a.mp3") is not None
    assert sons.resolve_som("../config.json") is None
    assert sons.resolve_som(str(paths.data_dir() / "secrets.json")) is None
    assert sons.resolve_som("") is None
