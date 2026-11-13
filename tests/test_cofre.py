import base64, json, logging

import aiohttp
import pytest

from livehub import cofre, settings
from test_e2e_config import app  # noqa: F401  (fixture do runtime real)

pytestmark = pytest.mark.skipif(not cofre.disponivel(), reason="DPAPI so no Windows")
SEG = "segredo-super-secreto-123"


def test_roundtrip():
    c = cofre.proteger("olá".encode())
    assert c != "olá".encode() and cofre.abrir(c).decode() == "olá"


def test_save_cifra_e_load_abre(data_dir):
    sec = settings.load_secrets()
    sec["livepix"]["client_secret"] = SEG
    settings.save_secrets(sec)
    raw = (data_dir / "secrets.json").read_bytes()
    assert SEG.encode() not in raw and json.loads(raw)["formato"] == "dpapi-v1"
    assert settings.load_secrets()["livepix"]["client_secret"] == SEG


def test_legado_migra(data_dir, caplog):
    (data_dir / "secrets.json").write_text(json.dumps({"livepix": {"client_id": "i", "client_secret": SEG}}), encoding="utf-8")
    with caplog.at_level(logging.INFO):
        assert settings.load_secrets()["livepix"]["client_secret"] == SEG
    raw = (data_dir / "secrets.json").read_bytes()
    assert SEG.encode() not in raw and json.loads(raw)["formato"] == "dpapi-v1"
    assert "segredos migrados para arquivo criptografado" in caplog.text
    assert settings.load_secrets()["livepix"]["client_id"] == "i"


def test_corrompido_isola_e_devolve_padroes(data_dir):
    (data_dir / "secrets.json").write_text(json.dumps(
        {"formato": "dpapi-v1", "dados": base64.b64encode(b"lixo").decode()}), encoding="utf-8")
    sec = settings.load_secrets()
    assert sec["livepix"] == settings.DEFAULT_SECRETS["livepix"]
    assert len(list(data_dir.glob("secrets.json.ilegivel-*"))) == 1
    assert settings.load_secrets()["livepix"]["client_id"] == ""  # recriado, nao levanta


async def test_post_config_loga_sem_valores(app, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    async with aiohttp.ClientSession() as s:
        r = await s.post(app.url + "/api/config", json={"livepix": {"client_id": "idxyz987", "client_secret": SEG}})
        assert r.status == 200
        r = await s.post(app.url + "/api/config", json={"livepix": {"intervalo": 2}})
        assert r.status == 400
    t = caplog.text
    assert "config salva: ['livepix']" in t and "livepix: client_id alterado, client_secret alterado" in t
    assert "config recusada: campo=livepix.intervalo" in t
    assert SEG not in t and "idxyz987" not in t
    async with aiohttp.ClientSession() as s:
        await s.post(app.url + "/api/config", json={"livepix": {"remover": True}})
    assert "livepix: credenciais removidas" in caplog.text
