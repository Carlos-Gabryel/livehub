import base64, hashlib, io, json, subprocess, sys, zipfile
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from livehub import __version__, atualizacao, settings
from livehub.atualizacao import Atualizador, mais_nova, versao_tupla
from livehub.runtime import Runtime
from conftest import porta_livre


def test_versao_tupla():
    assert versao_tupla("v0.2.0") == (0, 2, 0) and versao_tupla("0.2.0") == (0, 2, 0)
    assert versao_tupla("1.10") == (1, 10)
    for ruim in ("", "abc", "1.x", "v", None, "1..2", "-1.0"):
        assert versao_tupla(ruim) is None


def test_mais_nova():
    assert mais_nova("v0.2.1", "0.2.0") and mais_nova("0.10.0", "0.9.9") and mais_nova("1.0", "0.9.9")
    assert not mais_nova("0.2.0", "0.2.0") and not mais_nova("0.1.9", "0.2.0")
    assert not mais_nova("lixo", "0.2.0")


CHAVE = Ed25519PrivateKey.generate()
PUB_B64 = base64.b64encode(CHAVE.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


@pytest.fixture(autouse=True)
def chave_teste(monkeypatch):
    monkeypatch.setattr(atualizacao, "CHAVE_PUBLICA", PUB_B64)


def assina(ver, sha):
    return base64.b64encode(CHAVE.sign(f"livehub|{ver}|{sha}".encode())) + b"\n"


def zip_bytes(com_exe=True):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        if com_exe:
            z.writestr("LiveHub/LiveHub.exe", "exe9.9.9")
        z.writestr("LiveHub/LEIA-ME.txt", "x")
    return b.getvalue()


class FakeGH:
    def __init__(self, tag="v9.9.9", sha_ok=True, prerelease=False, com_assets=True, com_exe=True, sig="ok"):
        self.tag, self.prerelease, self.com_assets = tag, prerelease, com_assets
        self.data = zip_bytes(com_exe)
        h = hashlib.sha256(self.data).hexdigest() if sha_ok else "0" * 64
        self.nome = f"LiveHub-{tag.lstrip('v')}-windows.zip"
        self.sha = f"{h}  {self.nome}\n".encode()
        ver = tag.lstrip("v")
        orig = hashlib.sha256(self.data).hexdigest()
        if sig == "adulterado":  # zip trocado, sha casa com o trocado, assinatura e do original
            self.data = self.data + b"malware"
            self.sha = f"{hashlib.sha256(self.data).hexdigest()}  {self.nome}\n".encode()
        self.sigb = {"ok": assina(ver, orig), "adulterado": assina(ver, orig),
                     "outra_versao": assina("1.0.0", orig), "outro_sha": assina(ver, "0" * 64),
                     "lixo": b"!!nao-base64!!"}.get(sig)
        self.hits = []
        app = web.Application()
        app.router.add_get("/latest", self.latest)

        async def zipr(r):
            return web.Response(body=self.data)

        async def shar(r):
            return web.Response(body=self.sha)
        app.router.add_get("/zip", zipr)
        app.router.add_get("/sha", shar)

        async def sigr(r):
            return web.Response(body=self.sigb)
        app.router.add_get("/sig", sigr)
        self.runner = web.AppRunner(app)

    async def latest(self, req):
        self.hits.append(req.headers.get("User-Agent"))
        b = self.base
        ass = [{"name": self.nome, "browser_download_url": b + "/zip"},
               {"name": self.nome + ".sha256", "browser_download_url": b + "/sha"}] if self.com_assets else []
        if self.com_assets and self.sigb is not None:
            ass.append({"name": self.nome + ".sig", "browser_download_url": b + "/sig"})
        return web.json_response({"tag_name": self.tag, "prerelease": self.prerelease, "draft": False,
                                  "body": "notas", "html_url": "https://github.com/Carlos-Gabryel/livehub/releases/tag/v9.9.9", "assets": ass})

    async def start(self):
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self.base + "/latest"

    async def stop(self):
        await self.runner.cleanup()


@pytest.fixture
def instalado(tmp_path, monkeypatch):
    d = tmp_path / "inst" / "LiveHub"
    d.mkdir(parents=True)
    (d / "LiveHub.exe").write_text("old")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(d / "LiveHub.exe"))
    return d


async def rodar(monkeypatch, **kw):
    gh = FakeGH(**kw)
    monkeypatch.setenv("LIVEHUB_UPDATE_URL", await gh.start())
    a = Atualizador()
    try:
        e = await a.verificar()
    finally:
        await gh.stop()
    return a, e, gh


async def test_baixa_confere_e_extrai(instalado, monkeypatch, data_dir):
    antiga = data_dir / "update" / "0.0.5"
    antiga.mkdir(parents=True)
    avisos = []
    gh = FakeGH()
    monkeypatch.setenv("LIVEHUB_UPDATE_URL", await gh.start())
    a = Atualizador()
    a.on_pronto = avisos.append
    e = await a.verificar()
    await a.verificar()
    await gh.stop()
    assert e["estado"] == "pronto" and e["disponivel"] == "9.9.9" and e["notas"] == "notas"
    assert (data_dir / "update" / "9.9.9" / "staged" / "LiveHub" / "LiveHub.exe").read_text() == "exe9.9.9"
    assert not antiga.exists()
    assert avisos == ["9.9.9"]  # uma vez por versao
    assert gh.hits[0] == f"LiveHub/{__version__}"


async def test_sha_errado(instalado, monkeypatch, data_dir):
    _, e, _ = await rodar(monkeypatch, sha_ok=False)
    assert e["estado"] == "erro"
    assert not (data_dir / "update" / "9.9.9").exists()


async def test_marcador_verificado(instalado, monkeypatch, data_dir):
    a, e, _ = await rodar(monkeypatch)
    assert (data_dir / "update" / "9.9.9" / "verificado.txt").read_text() == "9.9.9"


async def test_sem_sig(instalado, monkeypatch, data_dir):
    _, e, _ = await rodar(monkeypatch, sig=None)
    assert e["estado"] == "erro" and e["erro"] == "Atualização sem assinatura — baixe manualmente"
    assert not (data_dir / "update" / "9.9.9").exists()


@pytest.mark.parametrize("sig", ["outra_versao", "outro_sha", "adulterado", "lixo"])
async def test_assinatura_invalida(instalado, monkeypatch, data_dir, sig):
    _, e, _ = await rodar(monkeypatch, sig=sig)
    assert e["estado"] == "erro" and e["erro"] == "Assinatura inválida — atualização recusada"
    assert not (data_dir / "update" / "9.9.9").exists()


async def test_chave_errada(instalado, monkeypatch, data_dir):
    outra = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(atualizacao, "CHAVE_PUBLICA", base64.b64encode(outra).decode())
    _, e, _ = await rodar(monkeypatch)
    assert e["estado"] == "erro" and not (data_dir / "update" / "9.9.9").exists()


async def test_preparar_exige_verificado(instalado, monkeypatch, data_dir):
    a, e, _ = await rodar(monkeypatch)
    assert e["estado"] == "pronto"
    (data_dir / "update" / "9.9.9" / "verificado.txt").unlink()
    chamadas = []
    monkeypatch.setattr(atualizacao.subprocess, "Popen", lambda *x, **k: chamadas.append(1))
    assert a.preparar_instalacao() is False and not chamadas


async def test_staged_sem_marcador_rebaixa(instalado, monkeypatch, data_dir):
    d = data_dir / "update" / "9.9.9" / "staged" / "LiveHub"
    d.mkdir(parents=True)
    (d / "LiveHub.exe").write_text("planted")
    _, e, _ = await rodar(monkeypatch)
    assert e["estado"] == "pronto"
    assert (d / "LiveHub.exe").read_text() == "exe9.9.9"


async def test_zip_sem_exe(instalado, monkeypatch, data_dir):
    _, e, _ = await rodar(monkeypatch, com_exe=False)
    assert e["estado"] == "erro" and not (data_dir / "update" / "9.9.9").exists()


async def test_sem_assets(instalado, monkeypatch):
    _, e, _ = await rodar(monkeypatch, com_assets=False)
    assert e["estado"] == "erro"


@pytest.mark.parametrize("tag", [f"v{__version__}", "v0.0.1"])
async def test_sem_versao_nova(instalado, monkeypatch, tag):
    _, e, _ = await rodar(monkeypatch, tag=tag)
    assert e["estado"] == "nada" and e["disponivel"] == ""


async def test_prerelease_ignorada(instalado, monkeypatch):
    _, e, _ = await rodar(monkeypatch, prerelease=True)
    assert e["estado"] == "nada"


async def test_nao_congelado_e_dev(monkeypatch, data_dir):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    _, e, _ = await rodar(monkeypatch)
    assert e["estado"] == "dev" and e["disponivel"] == "9.9.9" and e["url_release"].startswith("https://github.com/Carlos-Gabryel/")
    assert not (data_dir / "update" / "9.9.9").exists()


async def test_sem_permissao(instalado, monkeypatch):
    monkeypatch.setattr(atualizacao, "_gravavel", lambda d: False)
    _, e, _ = await rodar(monkeypatch)
    assert e["estado"] == "sem_permissao"


async def test_rede_falha(instalado, monkeypatch):
    monkeypatch.setenv("LIVEHUB_UPDATE_URL", "http://127.0.0.1:1/x")
    e = await Atualizador().verificar()
    assert e["estado"] == "erro"


def test_marcar_ok_limpa_antigas(data_dir):
    u = data_dir / "update"
    for v in ("0.1.0", __version__, "99.0.0"):
        (u / v).mkdir(parents=True)
    Atualizador().marcar_ok()
    assert (u / "ok.txt").read_text() == __version__
    assert not (u / "0.1.0").exists() and not (u / __version__).exists() and (u / "99.0.0").exists()


# ---- migracao de settings ----
def test_settings_ganha_versao(data_dir):
    settings.config_path().write_text(json.dumps({"web_port": 9001}), encoding="utf-8")
    cfg = settings.load_config()
    assert cfg["settings_version"] == 1 and cfg["web_port"] == 9001
    assert json.loads(settings.config_path().read_text(encoding="utf-8"))["settings_version"] == 1


def test_migracao_roda_uma_vez(data_dir, monkeypatch):
    chamadas = []

    def m2(cfg):
        chamadas.append(1)
        cfg["web_port"] += 1
        return cfg
    monkeypatch.setattr(settings, "MIGRACOES", {2: m2})
    settings.config_path().write_text(json.dumps({"web_port": 9001, "settings_version": 1}), encoding="utf-8")
    assert settings.load_config()["web_port"] == 9002
    assert settings.load_config()["web_port"] == 9002
    assert len(chamadas) == 1 and settings.load_config()["settings_version"] == 2


# ---- aplicador PowerShell ----
PS1 = Path(atualizacao.__file__).with_name("atualizacao_aplicar.ps1")


def _versao(d: Path, v, grava_marker, marker):
    d.mkdir(parents=True)
    (d / "versao.txt").write_text(v)
    corpo = "@echo off\r\n" + (f'echo ok>"{marker}"\r\n' if grava_marker else "")
    (d / "LiveHub.cmd").write_text(corpo)


def _aplicar(tmp_path, v2_ok, timeout):
    base = tmp_path / "ps"
    marker, log = base / "ok.txt", base / "aplicar.log"
    inst, staged = base / "LiveHub", base / "staged" / "LiveHub"
    base.mkdir()
    _versao(inst, "v1", True, marker)
    _versao(staged, "v2", v2_ok, marker)
    morto = subprocess.Popen([sys.executable, "-c", "pass"])
    morto.wait()
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1),
                        "-ProcId", str(morto.pid), "-Install", str(inst), "-Staged", str(staged),
                        "-Marker", str(marker), "-Log", str(log), "-TimeoutOk", str(timeout),
                        "-ExeName", "LiveHub.cmd"], capture_output=True, text=True, timeout=120)
    return inst, base, r


@pytest.mark.skipif(sys.platform != "win32", reason="so Windows")
def test_ps1_aplica_e_remove_old(tmp_path):
    inst, base, r = _aplicar(tmp_path, True, 20)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (inst / "versao.txt").read_text() == "v2"
    assert not (base / "LiveHub.old").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="so Windows")
def test_ps1_rollback_sem_marker(tmp_path):
    inst, base, r = _aplicar(tmp_path, False, 3)
    assert r.returncode == 2, r.stdout + r.stderr
    assert (inst / "versao.txt").read_text() == "v1"
    assert not (base / "LiveHub.old").exists()
    assert "rollback" in (base / "aplicar.log").read_text(encoding="utf-8")


# ---- rotas ----
@pytest.fixture
async def rt():
    cfg = settings.load_config()
    cfg["web_port"] = porta_livre()
    settings.save_config(cfg)
    r = Runtime()
    await r.start()
    yield r
    await r.stop()


async def test_rotas_update(rt, monkeypatch):
    b = f"http://127.0.0.1:{rt.cfg['web_port']}"
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    gh = FakeGH()
    monkeypatch.setenv("LIVEHUB_UPDATE_URL", await gh.start())
    async with aiohttp.ClientSession() as s:
        async with s.get(b + "/api/update") as r:
            j = await r.json()
            assert j["estado"] == "nada" and j["atual"] == __version__ and j["modo"] == "avisar"
        async with s.post(b + "/api/update/verificar", json={}) as r:
            assert (await r.json())["estado"] == "dev"
        async with s.post(b + "/api/update/instalar", json={}) as r:
            j = await r.json()
            assert j["ok"] is False and j["estado"] == "dev"
        async with s.post(b + "/api/config", json={"atualizacao": {"verificar": False, "modo": "ao_sair"}}) as r:
            assert r.status == 200
        assert settings.load_config()["atualizacao"] == {"verificar": False, "modo": "ao_sair"}
        for ruim in ({"modo": "x"}, {"verificar": "sim"}):
            async with s.post(b + "/api/config", json={"atualizacao": ruim}) as r:
                assert r.status == 400
    await gh.stop()


# ---- allowlist de hosts / url_release ----
@pytest.mark.parametrize("u,ok", [
    ("https://api.github.com/repos/x", True), ("https://github.com/a/b", True),
    ("https://objects.githubusercontent.com/x", True), ("https://githubusercontent.com/x", False),
    ("http://github.com/a", False), ("https://evil.com/github.com", False),
    ("https://github.com.evil.com/a", False), ("https://evilgithubusercontent.com/a", False),
    ("ftp://github.com/a", False), ("", False)])
def test_host_permitido(u, ok):
    assert atualizacao.host_permitido(u) is ok


class _Resp:
    def __init__(self, url, corpo, hist=()):
        self.url, self.history, self._c = url, tuple(hist), corpo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self, **k):
        return self._c

    async def read(self):
        return self._c

    @property
    def content(self):
        c = self._c

        class It:
            async def iter_chunked(self, n):
                yield c
        return It()


class _Ses:
    """Substitui aiohttp.ClientSession: mapa url -> (corpo, historico de redirects, url final)."""
    def __init__(self, mapa, visitadas):
        self.mapa, self.v = mapa, visitadas

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url):
        self.v.append(url)
        corpo, hist, final = self.mapa[url]
        return _Resp(final or url, corpo, [type("H", (), {"url": h}) for h in hist])


def _release(base, **ass_url):
    nome = "LiveHub-9.9.9-windows.zip"
    ass = [{"name": nome + suf, "browser_download_url": ass_url.get(suf, f"{base}/{suf or 'zip'}")}
           for suf in ("", ".sha256", ".sig")]
    return {"tag_name": "v9.9.9", "draft": False, "prerelease": False, "body": "", "html_url": "https://evil.com/x",
            "assets": ass}


API = "https://api.github.com/repos/Carlos-Gabryel/livehub/releases/latest"
BASE = "https://github.com/Carlos-Gabryel/livehub/releases/download/v9.9.9"


async def _ses_falsa(monkeypatch, mapa):
    monkeypatch.delenv("LIVEHUB_UPDATE_URL", raising=False)
    vis = []
    monkeypatch.setattr(atualizacao.aiohttp, "ClientSession", _Ses(mapa, vis))
    return vis


async def test_host_api_nao_github(instalado, monkeypatch):
    monkeypatch.setattr(atualizacao, "URL_PADRAO", "https://evil.com/latest")
    vis = await _ses_falsa(monkeypatch, {"https://evil.com/latest": ({}, (), None)})
    e = await Atualizador().verificar()
    assert e["estado"] == "erro" and vis == []


async def test_host_api_redirect_para_http(instalado, monkeypatch):
    await _ses_falsa(monkeypatch, {API: (_release(BASE), (), "http://github.com/x")})
    e = await Atualizador().verificar()
    assert e["estado"] == "erro"


@pytest.mark.parametrize("ruim", ["http://github.com/a/zip", "https://evil.com/a/zip"])
async def test_host_asset_invalido(instalado, monkeypatch, data_dir, ruim):
    rel = _release(BASE, **{"": ruim})
    vis = await _ses_falsa(monkeypatch, {API: (rel, (), None)})
    e = await Atualizador().verificar()
    assert e["estado"] == "erro" and vis == [API]
    assert not (data_dir / "update" / "9.9.9").exists()


def _mapa_ok(z, sha):
    return {API: (_release(BASE), (), None), f"{BASE}/.sha256": (f"{sha}  x".encode(), (), None),
            f"{BASE}/.sig": (assina("9.9.9", sha), (), None)}


async def test_host_asset_redirect_ruim(instalado, monkeypatch, data_dir):
    z = zip_bytes()
    mapa = _mapa_ok(z, hashlib.sha256(z).hexdigest())
    for hist, final in ((("https://evil.com/r",), "https://objects.githubusercontent.com/z"),
                        ((), "https://evil.com/z")):
        mapa[f"{BASE}/zip"] = (z, hist, final)
        await _ses_falsa(monkeypatch, mapa)
        e = await Atualizador().verificar()
        assert e["estado"] == "erro" and not (data_dir / "update" / "9.9.9").exists()


async def test_host_github_ok_com_redirect(instalado, monkeypatch, data_dir):
    z = zip_bytes()
    mapa = _mapa_ok(z, hashlib.sha256(z).hexdigest())
    mapa[f"{BASE}/zip"] = (z, (BASE + "/zip",), "https://release-assets.githubusercontent.com/z")
    await _ses_falsa(monkeypatch, mapa)
    e = await Atualizador().verificar()
    assert e["estado"] == "pronto", e
    assert e["url_release"] == atualizacao.URL_RELEASES  # html_url evil.com descartada


async def test_url_release_github_mantida(instalado, monkeypatch):
    tagurl = "https://github.com/Carlos-Gabryel/livehub/releases/tag/v9.9.9"
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    rel = _release(BASE)
    rel["html_url"] = tagurl
    await _ses_falsa(monkeypatch, {API: (rel, (), None)})
    e = await Atualizador().verificar()
    assert e["estado"] == "dev" and e["url_release"] == tagurl


# ---- tools/assinar.py ----
def test_assinar_roundtrip(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import assinar
    kp = tmp_path / "k.pem"
    kp.write_bytes(CHAVE.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    z = tmp_path / "LiveHub-1.2.3-windows.zip"
    z.write_bytes(zip_bytes())
    monkeypatch.setenv("LIVEHUB_SIGN_KEY", str(kp))
    out = assinar.assinar(z, "1.2.3")
    assert out == Path(str(z) + ".sig")
    sig = out.read_text(encoding="ascii")
    sha = hashlib.sha256(z.read_bytes()).hexdigest()
    assert atualizacao.verificar_assinatura("1.2.3", sha, sig)
    assert not atualizacao.verificar_assinatura("1.2.4", sha, sig)
    monkeypatch.setenv("LIVEHUB_SIGN_KEY", str(tmp_path / "nao.pem"))
    with pytest.raises(SystemExit):
        assinar.assinar(z, "1.2.3")
