"""Fila de alertas + rotas (overlay /alertas, WS, midia, controles). Config lida ao vivo de rt.cfg."""
import asyncio, collections, json, logging, os, random, re, sys, time, uuid
from pathlib import Path
from urllib.parse import quote

from aiohttp import WSMsgType, web

from .. import midia as midia_mod, paths
from ..resgates import const
from ..resgates.sons import listar_sons
from ..web import origem_local, seguro
from . import regras, tts

log = logging.getLogger("alertas")

FILA_MAX = 50
FILA_SEGUIDORES = 10  # acima disso, novos follows sao descartados (protecao contra bot de follow)
PULAR_INTERVALO = 3  # s: ignora um segundo pular logo apos o anterior (cliques duplos / dois docks)
EXTRA_TIMEOUT = 30  # folga alem da duracao (cobre o TTS)
IMG_EXTS, VIDEO_EXTS = midia_mod.IMG_EXTS, midia_mod.VIDEO_EXTS
VIDEO_MAX = 60  # s: tempo extra maximo que um video pode segurar o alerta
AMOSTRAS = {
    "follow": ("follow", ""), "sub": ("sub", "Tier 1, 3 meses"), "presente": ("presente de sub", "5 subs"),
    "raid": ("raid", "42 viewers"), "cheer": ("bits", "100 bits"), "doacao": ("doação", "R$ 10,00"),
}
MENSAGENS_TESTE = [
    "oi!", "salve salve", "boa live!", "primeiro!", "tmj demais", "manda um salve pra minha mãe!",
    "Amo suas lives, continua assim!", "Esse jogo é muito bom, quando vai zerar?",
    "Acabei de chegar, o que eu perdi?", "Parabéns pelo conteúdo, você merece muito mais!",
    "Fiz uma doação pequena mas é de coração, valeu por alegrar as minhas noites.",
    "Hoje o dia foi pesado no trabalho, mas a sua live me fez rir bastante. Obrigado por isso!",
    "Quero ver aquela jogada incrível de novo, foi a melhor da semana inteira, sem brincadeira.",
    "Mandando um abraço pra todo mundo do chat, vocês são demais! Bora que bora, GG!",
    "Será que você consegue passar dessa fase sem morrer nenhuma vez? Acredito em você, vai com tudo!",
    "Faz tempo que acompanho o canal e finalmente consegui ajudar. Continue com esse trabalho incrível, a comunidade só cresce!",
    "Meu aniversário é amanhã e o melhor presente seria ouvir você falando meu nome na live. Um abraço grande de Minas Gerais!",
    "Pessoal, não esqueçam de beber água e esticar as pernas! Cuidem da saúde, porque jogar bem também depende de estar bem.",
    "Descobri o canal por acaso numa madrugada e fiquei até o fim. Agora não perco mais nenhuma live, o astral aqui é sensacional!",
    "Boa noite, streamer! Passando para deixar meu apoio e dizer que seus vídeos me ajudaram muito a passar por um momento difícil. Obrigado de verdade por tudo, viu?",
    "Já são três da manhã e eu ainda estou aqui assistindo. Essa live está boa demais para dormir, então vou ficando mais um pouco, pode continuar!",
    "Gostaria de pedir uma música para a próxima pausa, algo animado para levantar o astral. Se puder tocar aquela do início do canal, ficaria muito feliz!",
    "Hoje consegui meu primeiro salário e a primeira coisa que fiz foi apoiar quem me ensina e diverte todo dia. Muito obrigado por existir, de coração. Vai ter mais, pode contar comigo sempre que der!",
    "Isso aqui não é só uma live, é uma comunidade, e eu me sinto em casa toda vez que entro. Valeu por criar esse espaço tão acolhedor para todo mundo que ama jogar e conversar sobre qualquer assunto, sem julgamento nenhum.",
    "Atenção, chat: se vocês gostam do conteúdo, deixem o seguir e compartilhem com os amigos! Quanto mais gente, mais divertido fica. E você, streamer, não desanima nunca, porque o seu esforço aparece em cada detalhe das lives. Força e foco!",
    "kkkkk não acredito que você caiu de novo naquele buraco",
]
USUARIOS_TESTE = ["Fulaninho", "Ana", "Pedro_gamer", "Bia", "Zé da Live", "Marquinhos"]


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


def resolver_video(nome) -> Path | None:
    return _dentro(paths.videos_dir(), nome, VIDEO_EXTS)


def resolver_tts(nome) -> Path | None:
    return _dentro(tts.tts_dir(), nome, {".mp3", ".wav"})


def listar_imagens() -> list[str]:
    root = paths.imagens_dir().resolve()
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.suffix.lower() in IMG_EXTS)


def listar_videos() -> list[str]:
    root = paths.videos_dir().resolve()
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTS)


def _url(prefixo, nome) -> str:
    return prefixo + quote(nome.replace("\\", "/"), safe="/") if nome else ""


class Alertas:
    def __init__(self, rt):
        self.rt = rt
        self.fila = collections.deque()
        self.clientes = set()
        self.atual = None
        self.pausado = False
        self._ultimo_pular = None
        self._avisos = set()
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
        return self._enfileirar(ev, cat, manual, forcar_tts) is not None

    def _enfileirar(self, ev, cat=None, manual=False, forcar_tts=False):
        cat = cat or regras.categoria(ev)
        if cat is None:
            return None
        if len(self.fila) >= FILA_MAX or (not manual and cat == "follow" and len(self.fila) > FILA_SEGUIDORES):
            log.info("fila cheia (%d): alerta de %s descartado", len(self.fila), cat)
            return None
        cfg = self._cfg()
        c = cfg["categorias"][cat]
        msg = regras.mensagem(ev)
        som, img, vid = resolver_som(c["som"]), resolver_imagem(c["imagem"]), resolver_video(c.get("video") or "")
        mx = int(cfg["estilo"].get("mensagem_max") or 120)
        curta = regras.cortar(msg, mx)
        a = {"id": uuid.uuid4().hex[:12], "seq": ev.get("seq"), "categoria": cat,
             "texto": regras.texto(c["texto"], ev, mensagem_max=mx), "mensagem": curta, "user": ev.get("user") or "",
             "valor": ev.get("valor") or "",
             "som_url": _url("/alertas/midia/som/", c["som"]) if som else "",
             "imagem_url": _url("/alertas/midia/img/", c["imagem"]) if img and not vid else "",
             "video_url": _url("/alertas/midia/video/", c["video"]) if vid else "",
             "volume": c["volume"], "duracao": c["duracao"], "tts_url": "", "tts_volume": cfg["tts"]["volume"],
             "estilo": cfg["estilo"]}
        t = cfg["tts"]
        n = regras.numero(ev.get("valor"))
        quer_tts = bool((c.get("tts") or forcar_tts) and msg and (manual or not t["minimo"] or (n or 0) >= t["minimo"]))
        item = {"alerta": a, "pronto": asyncio.Event()}
        self.fila.append(item)
        if quer_tts:
            tk = asyncio.create_task(self._fazer_tts(item, msg, regras.texto(c["texto"], ev, sem_mensagem=True), dict(t)))
            self._tts_tasks.add(tk)
            tk.add_done_callback(self._tts_tasks.discard)
        else:
            item["pronto"].set()
        self._sinal.set()
        self._avisar()
        return item

    async def _fazer_tts(self, item, msg, anuncio, cfg_tts):
        """O item ja esta na fila (ordem preservada); o worker espera o audio ficar pronto."""
        try:
            async with self._tts_sem:  # no maximo 2 geracoes ao mesmo tempo (fila FIFO mantem a ordem)
                p = await tts.gerar(msg, cfg_tts, anuncio)
            if p:
                item["alerta"]["tts_url"] = "/alertas/tts/" + quote(p.name)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("TTS do alerta falhou")
        finally:
            item["pronto"].set()

    # ---- controles ----
    def _avisar(self):
        """Difunde o estado (pausa/fila/atual) para as paginas do feed; sem loop rodando nao faz nada."""
        try:
            t = asyncio.get_running_loop().create_task(self.rt.hub._feed({"type": "alertas", **self.estado()}))
        except RuntimeError:
            return
        self._avisos.add(t)
        t.add_done_callback(self._avisos.discard)

    def pausar(self, valor=None) -> bool:
        """valor None alterna; True/False define (idempotente)."""
        self.pausado = (not self.pausado) if valor is None else bool(valor)
        self._sinal.set()
        self._avisar()
        return self.pausado

    async def pular(self) -> bool:
        """False se o ultimo pular foi ha menos de PULAR_INTERVALO s."""
        agora = time.monotonic()
        if self._ultimo_pular is not None and agora - self._ultimo_pular < PULAR_INTERVALO:
            return False
        self._ultimo_pular = agora
        await self._enviar({"type": "pular"})
        self._fim.set()
        return True

    def repetir(self, seq) -> bool:
        """KeyError se nao achou o evento; ValueError se o tipo nao gera alerta."""
        ev = next((e for e in self.rt.hub.interacoes if e["seq"] == seq), None)
        if ev is None:
            raise KeyError(seq)
        cat = regras.categoria(ev)
        if cat is None:
            raise ValueError(ev.get("tipo"))
        return self.enfileirar(ev, cat, manual=True)

    async def testar(self, cat) -> dict:
        if cat not in AMOSTRAS:
            raise ValueError(cat)
        tipo, detalhe = AMOSTRAS[cat]
        valor = {"cheer": lambda: f"{random.choice([100, 250, 500, 1000, 5000])} bits",
                 "doacao": lambda: "R$ " + random.choice(["2,00", "5,00", "10,00", "25,50", "50,00", "100,00"])
                 }.get(cat, lambda: detalhe if cat in ("presente", "raid") else "")()
        com_msg = cat in ("cheer", "doacao", "sub")
        msg = random.choice(MENSAGENS_TESTE) if com_msg else ""
        ev = {"platform": "twitch", "tipo": tipo, "user": random.choice(USUARIOS_TESTE),
              "detalhe": detalhe if cat == "sub" else msg if com_msg else "", "valor": valor,
              "mensagem": msg if cat == "sub" else "", "id": None, "seq": None}
        item = self._enfileirar(ev, cat, manual=True, forcar_tts=com_msg)
        if item is None:
            return {"ok": False, "tts": False if com_msg else None}
        if not com_msg:
            return {"ok": True, "tts": None}
        # o teste sempre fala; avisa a pagina se na live essa categoria fica muda (TTS desligado) ou tem minimo
        live = {"tts_na_live": bool(self._cfg()["categorias"][cat].get("tts")), "tts_minimo": self._cfg()["tts"]["minimo"]}
        try:
            await asyncio.wait_for(asyncio.shield(item["pronto"].wait()), 25)
        except asyncio.TimeoutError:
            return {"ok": True, "tts": False, **live}
        return {"ok": True, "tts": bool(item["alerta"]["tts_url"]), **live}

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
        self._avisar()
        self._fim.clear()
        await self._enviar({"type": "alerta", "alerta": a})
        try:
            await asyncio.wait_for(self._fim.wait(), a["duracao"] + self.extra_timeout + (VIDEO_MAX if a.get("video_url") else 0))
        except asyncio.TimeoutError:
            log.warning("alerta %s sem confirmacao; seguindo", a["id"])
        self.atual = None
        self._avisar()


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

    @r.get("/alertas/midia/video/{nome:.+}")
    async def video(req):
        return arquivo(resolver_video(req.match_info["nome"]))

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
        return web.json_response({"sons": listar_sons(), "imagens": listar_imagens(), "videos": listar_videos(),
                                  "vozes_neural": tts.VOZES_NEURAL, "vozes_windows": svc._vozes_win})

    @r.post("/api/alertas/testar")
    async def testar(req):
        if not seguro(req):
            return proibido()
        b = await corpo(req)
        if b is None:
            return web.json_response({"erro": "JSON inválido."}, status=400)
        try:
            res = await svc.testar(b.get("categoria"))
        except ValueError:
            return web.json_response({"erro": "Categoria inválida."}, status=400)
        return web.json_response(res)

    @r.post("/api/alertas/pular")
    async def pular(req):
        if not seguro(req):
            return proibido()
        if not await svc.pular():
            return web.json_response({"ok": False, "motivo": "aguarde"}, status=429)
        return web.json_response({"ok": True})

    @r.post("/api/alertas/pausar")
    async def pausar(req):
        if not seguro(req):
            return proibido()
        b = await corpo(req)
        v = b.get("pausado") if b else None
        if v is not None and not isinstance(v, bool):
            return web.json_response({"erro": "Valor inválido."}, status=400)
        return web.json_response({"ok": True, "pausado": svc.pausar(v)})

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

    @r.post("/api/alertas/abrir_videos")
    async def abrir_videos(req):
        if not seguro(req):
            return proibido()
        if sys.platform != "win32" or not hasattr(os, "startfile"):
            return web.json_response({"erro": "Disponível só no Windows."}, status=501)
        os.startfile(str(paths.videos_dir()))
        return web.json_response({"ok": True})

    ERROS_UPLOAD = {"tipo": (415, "Tipo de arquivo não suportado (use áudio .mp3/.wav/.ogg/.flac, imagem .png/.jpg/.gif/.webp ou vídeo .mp4/.webm)."),
                    "nome": (400, "Nome de arquivo inválido."),
                    "grande": (413, "Arquivo grande demais (máximo 50 MB para sons e imagens, 500 MB para vídeos).")}

    @r.post("/api/midia/enviar")
    async def enviar(req):
        if not seguro(req, ("application/octet-stream",)):
            return proibido()
        nome = req.query.get("nome", "")
        try:
            res = await midia_mod.salvar(nome, req.content.iter_chunked(256 * 1024))
        except ValueError as e:
            st, msg = ERROS_UPLOAD.get(str(e), (400, "Envio inválido."))
            return web.json_response({"erro": msg}, status=st)
        log.info("midia enviada: %s %s", res["tipo"], re.sub(r"[\x00-\x1f\x7f]", "", res["nome"])[:80])
        return web.json_response({"ok": True, **res})

    app.add_routes(r)
