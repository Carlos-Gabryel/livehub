"""Fila de alertas + rotas (overlay /alertas, WS, midia, controles). Config lida ao vivo de rt.cfg."""
import asyncio, collections, json, logging, os, sys, uuid
from pathlib import Path
from urllib.parse import quote

from aiohttp import WSMsgType, web

from .. import paths
from ..resgates import const
from ..resgates.sons import listar_sons
from ..web import origem_local, seguro
from . import regras, tts

log = logging.getLogger("alertas")

FILA_MAX = 50
FILA_SEGUIDORES = 10  # acima disso, novos follows sao descartados (protecao contra bot de follow)
EXTRA_TIMEOUT = 30  # folga alem da duracao (cobre o TTS)
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".apng"}
AMOSTRAS = {
    "follow": ("follow", ""), "sub": ("sub", "Tier 1, 3 meses"), "presente": ("presente de sub", "5 subs"),
    "raid": ("raid", "42 viewers"), "cheer": ("bits", "100 bits"), "doacao": ("doação", "R$ 10,00"),
}


def _dentro(root: Path, nome, exts=None) -> Path | None:
    """Arquivo `nome` dentro de `root` (sem sair por .. ou caminho absoluto), com extensao permitida."""
    if not isinstance(nome, str) or not nome or "\x00" in nome or Path(nome).is_absolute():
        return None
    root = root.resolve()
    try:
        p = (root / nome).resolve()
    except (OSError, ValueError):
        return None
    if root in p.parents and p.is_file() and (exts is None or p.suffix.lower() in exts):
        return p
    return None


def resolver_som(nome) -> Path | None:
    return _dentro(paths.sons_dir(), nome, const.SOM_EXTS)


def resolver_imagem(nome) -> Path | None:
    return _dentro(paths.imagens_dir(), nome, IMG_EXTS)


def resolver_tts(nome) -> Path | None:
    return _dentro(tts.tts_dir(), nome, {".mp3", ".wav"})


def listar_imagens() -> list[str]:
    root = paths.imagens_dir().resolve()
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.suffix.lower() in IMG_EXTS)


def _url(prefixo, nome) -> str:
    return prefixo + quote(nome.replace("\\", "/"), safe="/") if nome else ""


class Alertas:
    def __init__(self, rt):
        self.rt = rt
        self.fila = collections.deque()
        self.clientes = set()
        self.atual = None
        self.pausado = False
        self.extra_timeout = EXTRA_TIMEOUT
        self._sinal = asyncio.Event()
        self._fim = asyncio.Event()
        self._task = None
        self._tts_tasks = set()
        self._vozes_win = None
        self._tts_sem = asyncio.Semaphore(2)

    # ---- ciclo de vida ----
    def iniciar(self):
        if self._ouvir not in self.rt.hub.ouvintes:
            self.rt.hub.ouvintes.append(self._ouvir)
        self._task = asyncio.create_task(self._loop(), name="alertas")

    async def parar(self):
        try:
            self.rt.hub.ouvintes.remove(self._ouvir)
        except ValueError:
            pass
        tarefas = [t for t in (self._task, *self._tts_tasks) if t]
        self._task = None
        for t in tarefas:
            t.cancel()
        await asyncio.gather(*tarefas, return_exceptions=True)

    def _cfg(self) -> dict:
        return self.rt.cfg["alertas"]

    # ---- entrada de eventos ----
    def _ouvir(self, ev):
        cfg = self._cfg()
        cat = regras.categoria(ev)
        if not cfg.get("ativo") or cat is None:
            return
        c = cfg["categorias"].get(cat) or {}
        if not c.get("ativo"):
            return
        mn = c.get("minimo") or 0
        if mn > 0 and (regras.numero(ev.get("valor")) or 0) < mn:
            return
        self.enfileirar(ev, cat)

    def enfileirar(self, ev, cat=None, manual=False, forcar_tts=False) -> bool:
        cat = cat or regras.categoria(ev)
        if cat is None:
            return False
        if len(self.fila) >= FILA_MAX or (not manual and cat == "follow" and len(self.fila) > FILA_SEGUIDORES):
            log.info("fila cheia (%d): alerta de %s descartado", len(self.fila), cat)
            return False
        cfg = self._cfg()
        c = cfg["categorias"][cat]
        msg = regras.mensagem(ev)
        som, img = resolver_som(c["som"]), resolver_imagem(c["imagem"])
        a = {"id": uuid.uuid4().hex[:12], "seq": ev.get("seq"), "categoria": cat,
             "texto": regras.texto(c["texto"], ev), "mensagem": msg, "user": ev.get("user") or "",
             "valor": ev.get("valor") or "",
             "som_url": _url("/alertas/midia/som/", c["som"]) if som else "",
             "imagem_url": _url("/alertas/midia/img/", c["imagem"]) if img else "",
             "volume": c["volume"], "duracao": c["duracao"], "tts_url": "", "tts_volume": cfg["tts"]["volume"],
             "estilo": cfg["estilo"]}
        t = cfg["tts"]
        n = regras.numero(ev.get("valor"))
        quer_tts = bool((c.get("tts") or forcar_tts) and msg and (manual or not t["minimo"] or (n or 0) >= t["minimo"]))
        item = {"alerta": a, "pronto": asyncio.Event()}
        self.fila.append(item)
        if quer_tts:
            tk = asyncio.create_task(self._fazer_tts(item, msg, a["user"], dict(t)))
            self._tts_tasks.add(tk)
            tk.add_done_callback(self._tts_tasks.discard)
        else:
            item["pronto"].set()
        self._sinal.set()
        return True

    async def _fazer_tts(self, item, msg, user, cfg_tts):
        """O item ja esta na fila (ordem preservada); o worker espera o audio ficar pronto."""
        try:
            async with self._tts_sem:  # no maximo 2 geracoes ao mesmo tempo (fila FIFO mantem a ordem)
                p = await tts.gerar(msg, cfg_tts, user)
            if p:
                item["alerta"]["tts_url"] = "/alertas/tts/" + quote(p.name)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("TTS do alerta falhou")
        finally:
            item["pronto"].set()

    # ---- controles ----
    def pausar(self) -> bool:
        self.pausado = not self.pausado
        self._sinal.set()
        return self.pausado

    async def pular(self):
        await self._enviar({"type": "pular"})
        self._fim.set()

    def repetir(self, seq) -> bool:
        """KeyError se nao achou o evento; ValueError se o tipo nao gera alerta."""
        ev = next((e for e in self.rt.hub.interacoes if e["seq"] == seq), None)
        if ev is None:
            raise KeyError(seq)
        cat = regras.categoria(ev)
        if cat is None:
            raise ValueError(ev.get("tipo"))
        return self.enfileirar(ev, cat, manual=True)

    def testar(self, cat, tts_forcado=False) -> bool:
        if cat not in AMOSTRAS:
            raise ValueError(cat)
        tipo, detalhe = AMOSTRAS[cat]
        valor = "100 bits" if cat == "cheer" else detalhe if cat in ("presente", "raid", "doacao") else ""
        if tipo in ("bits", "doação"):
            detalhe = "Mensagem de teste do LiveHub"
        elif cat in ("presente", "raid", "doacao"):
            detalhe = ""
        ev = {"platform": "twitch", "tipo": tipo, "user": "Teste", "detalhe": detalhe, "valor": valor,
              "id": None, "seq": None}
        return self.enfileirar(ev, cat, manual=True, forcar_tts=tts_forcado)

    def estado(self) -> dict:
        a = self.atual
        return {"pausado": self.pausado, "fila": len(self.fila), "clientes": len(self.clientes),
                "atual": {"id": a["id"], "texto": a["texto"]} if a else None}

    # ---- worker ----
    async def _enviar(self, msg):
        texto = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.clientes):
            try:
                await ws.send_str(texto)
            except Exception:  # noqa: BLE001
                self.clientes.discard(ws)

    def cliente_saiu(self, ws):
        self.clientes.discard(ws)
        if not self.clientes:
            self._fim.set()  # sem tela para mostrar: nao prende a fila

    def fim_do_cliente(self, id_):
        if self.atual and self.atual["id"] == id_:
            self._fim.set()

    async def _loop(self):
        while True:
            try:
                await self._passo()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("worker de alertas")
                await asyncio.sleep(1)

    async def _passo(self):
        while not self.fila or self.pausado:
            self._sinal.clear()
            await self._sinal.wait()
        if not self.clientes:
            self.fila.popleft()
            log.info("nenhuma tela de alertas conectada; alerta descartado (%d na fila)", len(self.fila))
            return
        item = self.fila[0]
        await item["pronto"].wait()
        if self.pausado or not self.fila or self.fila[0] is not item:
            return
        self.fila.popleft()
        a = self.atual = item["alerta"]
        self._fim.clear()
        await self._enviar({"type": "alerta", "alerta": a})
        try:
            await asyncio.wait_for(self._fim.wait(), a["duracao"] + self.extra_timeout)
        except asyncio.TimeoutError:
            log.warning("alerta %s sem confirmacao; seguindo", a["id"])
        self.atual = None


def add_routes(app: web.Application, rt):
    svc = rt.alertas
    r = web.RouteTableDef()

    def proibido():
        return web.json_response({"erro": "Origem não permitida."}, status=403)

    async def corpo(req):
        try:
            b = await req.json()
        except Exception:  # noqa: BLE001
            return None
        return b if isinstance(b, dict) else None

    @r.get("/alertas")
    async def pagina(req):
        return web.FileResponse(paths.resource_dir() / "alertas" / "static" / "alertas.html",
                                headers={"Cache-Control": "no-cache"})

    @r.get("/alertas/ws")
    async def ws_alertas(req):
        if not origem_local(req):
            return web.Response(status=403)
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(req)
        svc.clientes.add(ws)
        svc._sinal.set()
        try:
            async for m in ws:
                if m.type == WSMsgType.TEXT:
                    try:
                        d = json.loads(m.data)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("type") == "fim":
                        svc.fim_do_cliente(d.get("id"))
                elif m.type == WSMsgType.ERROR:
                    break
        finally:
            svc.cliente_saiu(ws)
        return ws

    def arquivo(p):
        return web.FileResponse(p, headers={"Cache-Control": "no-cache"}) if p else web.Response(status=404)

    @r.get("/alertas/midia/som/{nome:.+}")
    async def som(req):
        return arquivo(resolver_som(req.match_info["nome"]))

    @r.get("/alertas/midia/img/{nome:.+}")
    async def img(req):
        return arquivo(resolver_imagem(req.match_info["nome"]))

    @r.get("/alertas/tts/{nome:.+}")
    async def tts_arq(req):
        return arquivo(resolver_tts(req.match_info["nome"]))

    @r.get("/api/alertas/estado")
    async def estado(req):
        return web.json_response(svc.estado())

    @r.get("/api/alertas/midia")
    async def midia(req):
        if not origem_local(req) or req.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return web.Response(status=403)
        if svc._vozes_win is None:
            svc._vozes_win = await tts.vozes_windows()
        return web.json_response({"sons": listar_sons(), "imagens": listar_imagens(),
                                  "vozes_neural": tts.VOZES_NEURAL, "vozes_windows": svc._vozes_win})

    @r.post("/api/alertas/testar")
    async def testar(req):
        if not seguro(req):
            return proibido()
        b = await corpo(req)
        if b is None:
            return web.json_response({"erro": "JSON inválido."}, status=400)
        try:
            ok = svc.testar(b.get("categoria"), b.get("tts") is True)
        except ValueError:
            return web.json_response({"erro": "Categoria inválida."}, status=400)
        return web.json_response({"ok": ok})

    @r.post("/api/alertas/pular")
    async def pular(req):
        if not seguro(req):
            return proibido()
        await svc.pular()
        return web.json_response({"ok": True})

    @r.post("/api/alertas/pausar")
    async def pausar(req):
        if not seguro(req):
            return proibido()
        return web.json_response({"ok": True, "pausado": svc.pausar()})

    @r.post("/api/alertas/repetir")
    async def repetir(req):
        if not seguro(req):
            return proibido()
        b = await corpo(req)
        seq = b.get("seq") if b else None
        if isinstance(seq, bool) or not isinstance(seq, int):
            return web.json_response({"erro": "Evento inválido."}, status=400)
        try:
            ok = svc.repetir(seq)
        except (KeyError, ValueError):
            return web.json_response({"erro": "Esse evento não gera alerta."}, status=404)
        return web.json_response({"ok": ok})

    @r.post("/api/alertas/abrir_imagens")
    async def abrir_imagens(req):
        if not seguro(req):
            return proibido()
        if sys.platform != "win32" or not hasattr(os, "startfile"):
            return web.json_response({"erro": "Disponível só no Windows."}, status=501)
        os.startfile(str(paths.imagens_dir()))
        return web.json_response({"ok": True})

    app.add_routes(r)
