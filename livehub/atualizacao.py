"""Auto-update via GitHub Releases: verifica, baixa, confere SHA-256, prepara e aplica (ps1 fora da pasta de instalacao)."""
import asyncio, hashlib, logging, os, shutil, subprocess, sys
from pathlib import Path

import aiohttp
from aiohttp import web

from . import __version__, paths, settings
from .web import seguro

log = logging.getLogger("atualizacao")
URL_PADRAO = "https://api.github.com/repos/Carlos-Gabryel/livehub/releases/latest"
PRIMEIRA_VERIFICACAO = 30
INTERVALO = 6 * 3600
EXE = "LiveHub.exe"


def versao_tupla(s):
    """'v0.2.0' / '0.2.0' -> (0, 2, 0); invalido -> None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    try:
        t = tuple(int(p) for p in s.split("."))
    except ValueError:
        return None
    return t if t and all(n >= 0 for n in t) else None


def mais_nova(remota, local) -> bool:
    r, l = versao_tupla(remota), versao_tupla(local)
    return r is not None and l is not None and r > l


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _install_dir() -> Path:
    return Path(sys.executable).resolve().parent


def update_dir() -> Path:
    d = paths.data_dir() / "update"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gravavel(d: Path) -> bool:
    try:
        p = d / f".lh_w_{os.getpid()}.tmp"
        p.write_bytes(b"x")
        p.unlink()
        return True
    except OSError:
        return False


def _sha_arquivo(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _extrair(zip_path: Path, destino: Path):
    import zipfile
    tmp = destino.with_name(destino.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(destino, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
    if not (tmp / "LiveHub" / EXE).is_file():
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"zip sem LiveHub/{EXE}")
    os.replace(tmp, destino)


class Atualizador:
    def __init__(self, rt=None):
        self.rt = rt
        self.s = {"atual": __version__, "disponivel": "", "notas": "", "url_release": "",
                  "estado": "nada", "erro": "", "modo": "avisar"}
        self.on_pronto = None  # callback(versao), chamado uma vez por versao
        self._notificada = ""
        self._task = None
        self._lock = asyncio.Lock()

    def estado(self) -> dict:
        self.s["modo"] = self._cfg().get("modo", "avisar")
        return dict(self.s)

    def _cfg(self) -> dict:
        try:
            return settings.load_config()["atualizacao"]
        except Exception:  # noqa: BLE001
            return {"verificar": True, "modo": "avisar"}

    def _url(self) -> str:
        return os.environ.get("LIVEHUB_UPDATE_URL") or URL_PADRAO

    def _erro(self, msg):
        log.warning("atualizacao: %s", msg)
        self.s.update(estado="erro", erro=msg)

    def _nada(self):
        self.s.update(estado="nada", erro="", disponivel="", notas="", url_release="")

    # ---- verificar / baixar ----
    async def verificar(self) -> dict:
        async with self._lock:
            ant = dict(self.s)
            if ant["estado"] != "pronto":
                self.s["estado"] = "verificando"
            try:
                await self._verificar()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("verificacao falhou: %r", e)
                if ant["estado"] == "pronto":
                    self.s = ant  # rede caiu: continua com o que ja esta baixado
                else:
                    self.s.update(estado="erro", erro="Não consegui verificar atualizações.")
        return self.estado()

    async def _verificar(self):
        hdr = {"User-Agent": f"LiveHub/{__version__}", "Accept": "application/vnd.github+json"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=hdr) as ses:
            async with ses.get(self._url()) as r:
                r.raise_for_status()
                rel = await r.json(content_type=None)
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            return self._nada()
        tag = str(rel.get("tag_name") or "")
        if not mais_nova(tag, __version__):
            return self._nada()
        ver = ".".join(map(str, versao_tupla(tag)))
        self.s.update(disponivel=ver, notas=str(rel.get("body") or ""), erro="",
                      url_release=str(rel.get("html_url") or ""))
        if not _frozen():
            self.s["estado"] = "dev"
            return
        inst = _install_dir()
        if not (_gravavel(inst) and _gravavel(inst.parent)):
            self.s["estado"] = "sem_permissao"
            return
        nome = f"LiveHub-{ver}-windows.zip"
        ass = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets") or [] if isinstance(a, dict)}
        if not ass.get(nome) or not ass.get(nome + ".sha256"):
            return self._erro(f"release {tag} sem {nome} ou .sha256")
        pasta = update_dir() / ver
        if (pasta / "staged" / "LiveHub" / EXE).is_file():
            return self._pronto(ver)
        self.s["estado"] = "baixando"
        await self._baixar(hdr, ver, pasta, nome, ass[nome], ass[nome + ".sha256"])

    async def _baixar(self, hdr, ver, pasta, nome, url_zip, url_sha):
        shutil.rmtree(pasta, ignore_errors=True)
        pasta.mkdir(parents=True)
        zp, shap = pasta / nome, pasta / (nome + ".sha256")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800, connect=20, sock_read=60),
                                             headers=hdr) as ses:
                async with ses.get(url_sha) as r:
                    r.raise_for_status()
                    shap.write_bytes(await r.read())
                async with ses.get(url_zip) as r:
                    r.raise_for_status()
                    with open(zp, "wb") as f:
                        async for b in r.content.iter_chunked(1 << 16):
                            f.write(b)
            partes = shap.read_text(encoding="utf-8", errors="replace").split()
            esperado = partes[0].lower() if partes else ""
            if await asyncio.to_thread(_sha_arquivo, zp) != esperado:
                shutil.rmtree(pasta, ignore_errors=True)
                return self._erro("SHA-256 do download não confere; atualização descartada.")
            await asyncio.to_thread(_extrair, zp, pasta / "staged")
            zp.unlink(missing_ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(pasta, ignore_errors=True)
            return self._erro(f"falha ao baixar/extrair: {e!r}")
        self._limpar(manter=ver)
        self._pronto(ver)

    def _pronto(self, ver):
        self.s.update(estado="pronto", erro="")
        log.info("atualizacao %s pronta para instalar", ver)
        if self.on_pronto and self._notificada != ver:
            self._notificada = ver
            try:
                self.on_pronto(ver)
            except Exception:  # noqa: BLE001
                log.exception("on_pronto falhou")

    def _limpar(self, manter=None, ate=None):
        """Apaga pastas de versoes em update/ (exceto `manter`; com `ate`, so as <= ate)."""
        for d in update_dir().iterdir():
            if not d.is_dir() or d.name == manter:
                continue
            if ate is not None:
                t = versao_tupla(d.name)
                if t is None or t > versao_tupla(ate):
                    continue
            shutil.rmtree(d, ignore_errors=True)

    # ---- instalar ----
    def preparar_instalacao(self) -> bool:
        """Lanca o aplicador destacado; o chamador deve encerrar o app em seguida."""
        if self.s["estado"] != "pronto" or not _frozen():
            return False
        inst = _install_dir()
        if not (_gravavel(inst) and _gravavel(inst.parent)):
            self.s["estado"] = "sem_permissao"
            return False
        staged = update_dir() / self.s["disponivel"] / "staged" / "LiveHub"
        if not (staged / EXE).is_file():
            return False
        try:
            up = update_dir()
            ps1 = up / "aplicar.ps1"
            shutil.copyfile(paths.resource_dir() / "atualizacao_aplicar.ps1", ps1)
            marker = up / "ok.txt"
            marker.unlink(missing_ok=True)
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                   "-File", str(ps1), "-ProcId", str(os.getpid()), "-Install", str(inst),
                   "-Staged", str(staged), "-Marker", str(marker), "-Log", str(up / "aplicar.log")]
            flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
            subprocess.Popen(cmd, creationflags=flags, cwd=str(paths.data_dir()), close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("aplicador iniciado para %s", self.s["disponivel"])
            return True
        except Exception:  # noqa: BLE001
            log.exception("nao consegui iniciar o aplicador")
            return False

    def marcar_ok(self):
        """Chamado quando o runtime subiu: o aplicador espera este arquivo para confirmar a atualizacao."""
        try:
            (update_dir() / "ok.txt").write_text(__version__, encoding="utf-8")
            self._limpar(ate=__version__)
            log.info("versao %s iniciada (marcador gravado)", __version__)
        except OSError:
            log.exception("marcar_ok falhou")

    # ---- loop de fundo ----
    def iniciar(self):
        if os.environ.get("LIVEHUB_NO_UPDATE"):
            return
        self._task = asyncio.create_task(self._loop(), name="atualizacao")

    async def parar(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        await asyncio.sleep(PRIMEIRA_VERIFICACAO)
        while True:
            try:
                if self._cfg().get("verificar", True):
                    await self.verificar()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("loop de atualizacao")
            await asyncio.sleep(INTERVALO)


def add_routes(app: web.Application, rt):
    r = web.RouteTableDef()

    @r.get("/api/update")
    async def get_update(req):
        return web.json_response(rt.atualizador.estado())

    @r.post("/api/update/verificar")
    async def verificar(req):
        if not seguro(req):
            return web.json_response({"erro": "Origem não permitida."}, status=403)
        return web.json_response(await rt.atualizador.verificar())

    @r.post("/api/update/instalar")
    async def instalar(req):
        if not seguro(req):
            return web.json_response({"erro": "Origem não permitida."}, status=403)
        a = rt.atualizador
        if a.preparar_instalacao():
            pedir = getattr(rt, "pedir_saida", None)
            if pedir:
                asyncio.get_running_loop().call_later(0.7, pedir)  # deixa a resposta chegar ao navegador
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "estado": a.estado()["estado"]})

    app.add_routes(r)
