"""Pagina /config e API /api/config: configuracao completa pelo navegador, sem editar arquivos."""
import asyncio, copy, logging, os, re, sys

from aiohttp import web

from . import __version__, paths, settings
from .alertas import regras as alertas_regras, servico as alertas_servico
from .chat import livepix
from .chat.kick import Kick
from .resgates import audio
from .web import WEB_PORT, seguro

log = logging.getLogger("configweb")

RT = web.AppKey("runtime", object)
NOME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
TIMEOUT_KICK = 10.0


class Erro(Exception):
    def __init__(self, campo, msg):
        self.campo, self.msg = campo, msg


def _url_slug(v: str, dominios: tuple) -> str:
    """'https://www.kick.com/x/?a=1' -> 'x' (so quando parece URL); '@x' -> 'x'."""
    v = v.strip()
    m = re.match(r"^(?:https?://)?(?:www\.|m\.)?(" + "|".join(re.escape(d) for d in dominios) + r")/(.*)$", v, re.I)
    if m:
        v = m.group(2).split("?")[0].split("#")[0].strip("/").split("/")[0]
    return v.lstrip("@").strip()


def _texto(campo, v, dominios=None, nome=True):
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise Erro(campo, "Valor inválido.")
    v = v.strip()
    if len(v) > 200:
        raise Erro(campo, "Texto muito longo (máximo 200 caracteres).")
    if dominios:
        v = _url_slug(v, dominios)
    if nome and v and not NOME_RE.match(v):
        raise Erro(campo, "Use só letras, números, ponto, hífen e sublinhado.")
    return v


def _inteiro(campo, v, lo, hi, msg):
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise Erro(campo, msg)
    try:
        n = int(str(v).strip())
    except ValueError:
        raise Erro(campo, msg)
    if not lo <= n <= hi:
        raise Erro(campo, msg)
    return n


def _id_ou_nulo(campo, v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return _inteiro(campo, v, 1, 2**63 - 1, "Use um número inteiro positivo.")


def _intervalo(campo, v):
    return _inteiro(campo, v, 15, 600, "Use um valor entre 15 e 600 segundos.")


def _porta(campo, v):
    return _inteiro(campo, v, 1024, 65535, "Use uma porta entre 1024 e 65535.")


def _secao(body, nome):
    s = body.get(nome)
    if s is None:
        return {}
    if not isinstance(s, dict):
        raise Erro(nome, "Formato inválido.")
    return s


HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
FONTE_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,60}$")
VOZ_NEURAL_RE = re.compile(r"^[A-Za-z]{2,3}-[A-Za-z]{2,4}-[A-Za-z]+Neural$")
ANIMACOES = ("fade", "slide", "pop", "bounce")


def _bool(campo, v):
    if not isinstance(v, bool):
        raise Erro(campo, "Valor inválido.")
    return v


def _numero_min(campo, v):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise Erro(campo, "Use um número maior ou igual a 0.")
    try:
        n = float(str(v).strip().replace(",", "."))
    except ValueError:
        raise Erro(campo, "Use um número maior ou igual a 0.")
    if not 0 <= n <= 1e9:
        raise Erro(campo, "Use um número maior ou igual a 0.")
    return int(n) if n == int(n) else n


def _arquivo(campo, v, resolver):
    if v is None:
        v = ""
    if not isinstance(v, str) or len(v) > 300:
        raise Erro(campo, "Arquivo inválido.")
    v = v.strip()
    if v and resolver(v) is None:
        raise Erro(campo, "Arquivo não encontrado na pasta.")
    return v


def _alertas(a: dict, cfg: dict):
    al = cfg["alertas"]
    if "ativo" in a:
        al["ativo"] = _bool("alertas.ativo", a["ativo"])
    e = _secao(a, "estilo")
    est = al["estilo"]
    if "fonte" in e:
        if not isinstance(e["fonte"], str) or not (e["fonte"].strip() == "system-ui" or FONTE_RE.match(e["fonte"].strip())):
            raise Erro("alertas.estilo.fonte", "Nome de fonte inválido.")
        est["fonte"] = e["fonte"].strip()
    if "tamanho" in e:
        est["tamanho"] = _inteiro("alertas.estilo.tamanho", e["tamanho"], 12, 120, "Use um valor entre 12 e 120.")
    if "imagem_largura" in e:
        est["imagem_largura"] = _inteiro("alertas.estilo.imagem_largura", e["imagem_largura"], 40, 1600,
                                         "Use um valor entre 40 e 1600.")
    for k in ("cor_texto", "cor_destaque"):
        if k in e:
            if not isinstance(e[k], str) or not HEX_RE.match(e[k]):
                raise Erro(f"alertas.estilo.{k}", "Cor inválida (use o formato #RRGGBB).")
            est[k] = e[k].lower()
    if "contorno" in e:
        est["contorno"] = _bool("alertas.estilo.contorno", e["contorno"])
    if "animacao" in e:
        if e["animacao"] not in ANIMACOES:
            raise Erro("alertas.estilo.animacao", "Opção inválida.")
        est["animacao"] = e["animacao"]
    cats = _secao(a, "categorias")
    for nome, c in cats.items():
        if nome not in alertas_regras.CATEGORIAS:
            raise Erro(f"alertas.categorias.{nome}", "Categoria desconhecida.")
        if not isinstance(c, dict):
            raise Erro(f"alertas.categorias.{nome}", "Formato inválido.")
        d, pre = al["categorias"][nome], f"alertas.categorias.{nome}."
        for k in ("ativo", "tts"):
            if k in c:
                d[k] = _bool(pre + k, c[k])
        if "texto" in c:
            if not isinstance(c["texto"], str) or len(c["texto"].strip()) > 200 or not c["texto"].strip():
                raise Erro(pre + "texto", "Escreva um texto de até 200 caracteres.")
            d["texto"] = c["texto"].strip()
        if "som" in c:
            d["som"] = _arquivo(pre + "som", c["som"], alertas_servico.resolver_som)
        if "imagem" in c:
            d["imagem"] = _arquivo(pre + "imagem", c["imagem"], alertas_servico.resolver_imagem)
        if "volume" in c:
            d["volume"] = _inteiro(pre + "volume", c["volume"], 0, 100, "Use um valor entre 0 e 100.")
        if "duracao" in c:
            d["duracao"] = _inteiro(pre + "duracao", c["duracao"], 2, 30, "Use um valor entre 2 e 30 segundos.")
        if "minimo" in c:
            d["minimo"] = _numero_min(pre + "minimo", c["minimo"])
    t = _secao(a, "tts")
    d, pre = al["tts"], "alertas.tts."
    if "motor" in t:
        if t["motor"] not in ("neural", "windows"):
            raise Erro(pre + "motor", "Opção inválida.")
        d["motor"] = t["motor"]
    if "voz_neural" in t:
        if not isinstance(t["voz_neural"], str) or not VOZ_NEURAL_RE.match(t["voz_neural"].strip()):
            raise Erro(pre + "voz_neural", "Voz inválida.")
        d["voz_neural"] = t["voz_neural"].strip()
    if "voz_windows" in t:
        v = t["voz_windows"]
        if v is None:
            v = ""
        if not isinstance(v, str) or len(v.strip()) > 200:
            raise Erro(pre + "voz_windows", "Voz inválida.")
        d["voz_windows"] = v.strip()
    if "volume" in t:
        d["volume"] = _inteiro(pre + "volume", t["volume"], 0, 100, "Use um valor entre 0 e 100.")
    if "max_caracteres" in t:
        d["max_caracteres"] = _inteiro(pre + "max_caracteres", t["max_caracteres"], 20, 500, "Use um valor entre 20 e 500.")
    if "minimo" in t:
        d["minimo"] = _numero_min(pre + "minimo", t["minimo"])
    for k in ("ler_nome", "remover_links"):
        if k in t:
            d[k] = _bool(pre + k, t[k])
    if "palavras_bloqueadas" in t:
        pb = t["palavras_bloqueadas"]
        if not isinstance(pb, list) or len(pb) > 200 or not all(isinstance(x, str) and len(x) <= 60 for x in pb):
            raise Erro(pre + "palavras_bloqueadas", "Use até 200 palavras de até 60 caracteres.")
        d["palavras_bloqueadas"] = [x.strip() for x in pb if x.strip()]


def aplicar(body: dict, cfg: dict, sec: dict):
    """Aplica o corpo (parcial) validado em cfg/sec (modificados no lugar). Devolve info de mudancas."""
    info = {"kick_ids_dados": False, "kick_mudou": False, "credencial_mudou": False}

    t = _secao(body, "twitch")
    if "channel" in t:
        cfg["twitch"]["channel"] = _texto("twitch.channel", t["channel"], ("twitch.tv",)).lower()
    for k in ("client_id", "client_secret"):
        v = t.get(k)
        if v is None or v == "":
            continue  # vazio = manter
        if not isinstance(v, str) or len(v.strip()) > 200 or re.search(r"\s", v.strip()):
            raise Erro(f"twitch.{k}", "Valor inválido (sem espaços, máximo 200 caracteres).")
        v = v.strip()
        if v != sec["twitch"][k]:
            sec["twitch"][k] = v
            info["credencial_mudou"] = True
    if info["credencial_mudou"]:
        sec["twitch"].update(modo="app_proprio", access_token="", refresh_token="", expires_at=0,
                             user_id="", login="", login_expirado=False)

    k = _secao(body, "kick")
    if "channel" in k:
        novo = _texto("kick.channel", k["channel"], ("kick.com",)).lower()
        if novo != (cfg["kick"]["channel"] or "").lower():
            cfg["kick"].update(channel=novo, chatroom_id=None, channel_id=None)
            info["kick_mudou"] = True
    for campo in ("chatroom_id", "channel_id"):
        if campo in k:
            v = _id_ou_nulo(f"kick.{campo}", k[campo])
            if v is not None:
                info["kick_ids_dados"] = True
            cfg["kick"][campo] = v

    y = _secao(body, "youtube")
    if "handle" in y:
        h = _texto("youtube.handle", y["handle"], ("youtube.com",))
        if h.lower() in ("channel", "c", "user", "watch", "live"):
            raise Erro("youtube.handle", "Use o @ do canal (ex.: youtube.com/@meucanal).")
        cfg["youtube"]["handle"] = h
    if "check_interval" in y:
        cfg["youtube"]["check_interval"] = _intervalo("youtube.check_interval", y["check_interval"])

    tt = _secao(body, "tiktok")
    if "username" in tt:
        cfg["tiktok"]["username"] = _texto("tiktok.username", tt["username"], ("tiktok.com",))
    if "check_interval" in tt:
        cfg["tiktok"]["check_interval"] = _intervalo("tiktok.check_interval", tt["check_interval"])

    se = _secao(body, "streamelements")
    if se.get("remover") is True:
        sec["streamelements"]["jwt"] = ""
    elif se.get("jwt"):
        j = se["jwt"]
        if not isinstance(j, str) or len(j.strip()) > 4000 or re.search(r"\s", j.strip()):
            raise Erro("streamelements.jwt", "Token inválido (sem espaços).")
        sec["streamelements"]["jwt"] = j.strip()

    lp = _secao(body, "livepix")
    if "intervalo" in lp:
        cfg["livepix"]["intervalo"] = _inteiro("livepix.intervalo", lp["intervalo"], 5, 60,
                                               "Use um valor entre 5 e 60 segundos.")
    if lp.get("remover") is True:
        sec["livepix"].update(client_id="", client_secret="")
    else:
        for k in ("client_id", "client_secret"):
            v = lp.get(k)
            if v is None or v == "":
                continue  # vazio = manter
            if not isinstance(v, str) or len(v.strip()) > 200 or re.search(r"\s", v.strip()):
                raise Erro(f"livepix.{k}", "Valor inválido (sem espaços, máximo 200 caracteres).")
            sec["livepix"][k] = v.strip()

    sb = _secao(body, "streamerbot_compat")
    if "enabled" in sb:
        if not isinstance(sb["enabled"], bool):
            raise Erro("streamerbot_compat.enabled", "Valor inválido.")
        cfg["streamerbot_compat"]["enabled"] = sb["enabled"]
    if "port" in sb:
        cfg["streamerbot_compat"]["port"] = _porta("streamerbot_compat.port", sb["port"])

    if "web_port" in body:
        cfg["web_port"] = _porta("web_port", body["web_port"])
    if cfg["web_port"] == cfg["streamerbot_compat"]["port"]:
        raise Erro("streamerbot_compat.port", "A porta do Streamer.bot não pode ser igual à porta do LiveHub.")

    at = _secao(body, "atualizacao")
    if "verificar" in at:
        if not isinstance(at["verificar"], bool):
            raise Erro("atualizacao.verificar", "Valor inválido.")
        cfg["atualizacao"]["verificar"] = at["verificar"]
    if "modo" in at:
        if at["modo"] not in ("avisar", "ao_sair"):
            raise Erro("atualizacao.modo", "Valor inválido.")
        cfg["atualizacao"]["modo"] = at["modo"]

    _alertas(_secao(body, "alertas"), cfg)

    a = _secao(body, "audio")
    if "output_device" in a:
        dev = a["output_device"]
        if not isinstance(dev, str) or len(dev) > 200:
            raise Erro("audio.output_device", "Valor inválido.")
        cfg["audio"]["output_device"] = dev.strip()
    return info


async def resolver_kick(rt, slug):
    """(chatroom_id, channel_id) ou None se a API do Kick nao respondeu."""
    k = Kick({"kick": {"channel": slug}}, rt.hub, rt.session)
    try:
        await asyncio.wait_for(k.resolve_ids(), TIMEOUT_KICK)
    except Exception as e:
        log.warning("resolver ids do Kick falhou: %r", type(e).__name__)
        return None
    return k.chatroom_id, k.channel_id


def urls(porta):
    b = f"http://127.0.0.1:{porta}"
    return {"dock": b + "/dock", "eventos": b + "/eventos", "neutro": b + "/neutro",
            "recompensas": b + "/recompensas", "alertas": b + "/alertas"}


def estado(rt) -> dict:
    cfg, sec = settings.load_config(), settings.load_secrets()
    tw = sec["twitch"]
    try:
        saidas = audio.listar_saidas()
    except Exception as e:
        log.warning("listar saidas falhou: %r", e)
        saidas = []
    porta = rt.cfg["web_port"]
    try:
        assin = dict(rt.svc.eventsub.assinaturas)
    except AttributeError:
        assin = {"follow": "inativo", "resgates": "inativo"}
    return {
        "config": cfg,
        "status": dict(rt.hub.status),
        "twitch": {"client_id": tw["client_id"], "has_secret": bool(tw["client_secret"]),
                   "login": tw["login"], "user_id": tw["user_id"], "connected": bool(tw["refresh_token"]),
                   "modo": tw["modo"], "login_expirado": bool(tw.get("login_expirado")),
                   "assinaturas": assin},
        "streamelements": {"has_jwt": bool(sec["streamelements"]["jwt"])},
        "livepix": {"has_client_id": bool(sec["livepix"]["client_id"]), "has_secret": bool(sec["livepix"]["client_secret"])},
        "audio": {"saidas": saidas, "atual": cfg["audio"]["output_device"]},
        "sons_dir": str(paths.sons_dir()),
        "web_port": porta,
        "version": __version__,
        "urls": urls(porta),
    }


def _mudancas_segredos(antes: dict, depois: dict) -> list:
    """Linhas de log sobre quais segredos mudaram (nunca valores nem tamanhos)."""
    out = []
    for sec, chaves in (("twitch", ("client_id", "client_secret")), ("streamelements", ("jwt",)), ("livepix", ("client_id", "client_secret"))):
        a, d = antes[sec], depois[sec]
        mud = [k for k in chaves if a.get(k) != d.get(k)]
        if not mud:
            continue
        if all(not d.get(k) for k in mud):
            out.append(f"{sec}: credenciais removidas")
        else:
            out.append(f"{sec}: " + ", ".join(f"{k} alterado" for k in mud))
    return out


def add_routes(app: web.Application, rt):
    app[RT] = rt
    r = web.RouteTableDef()

    @r.get("/config")
    async def pagina(req):
        return web.FileResponse(paths.resource_dir() / "static" / "config.html")

    @r.get("/tema.css")
    async def tema(req):
        return web.FileResponse(paths.resource_dir() / "static" / "tema.css", headers={"Cache-Control": "no-cache"})

    @r.get("/api/config")
    async def get_config(req):
        return web.json_response(estado(rt))

    @r.post("/api/config")
    async def post_config(req):
        if not seguro(req):
            return web.json_response({"erro": "Origem não permitida."}, status=403)
        try:
            body = await req.json()
        except Exception:
            return web.json_response({"erro": "JSON inválido."}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"erro": "JSON inválido."}, status=400)
        cfg, sec = settings.load_config(), copy.deepcopy(settings.load_secrets())
        antes = copy.deepcopy(sec)
        try:
            info = aplicar(body, cfg, sec)
        except Erro as e:
            log.warning("config recusada: campo=%s erro=%s", e.campo, e.msg)
            return web.json_response({"campo": e.campo, "erro": e.msg}, status=400)
        kick = {"resolvido": None, "precisa_manual": False}
        k = cfg["kick"]
        if k["channel"] and not info["kick_ids_dados"] and not (k["chatroom_id"] and k["channel_id"]):
            ids = await resolver_kick(rt, k["channel"])
            if ids:
                k["chatroom_id"], k["channel_id"] = ids
                kick["resolvido"] = True
            else:
                kick.update(resolvido=False, precisa_manual=True)
        try:
            settings.save_secrets(sec)
            settings.save_config(cfg)
        except OSError as e:
            log.warning("falha ao salvar: %r", type(e).__name__)
            return web.json_response({"erro": "Não consegui salvar as configurações."}, status=500)
        log.info("config salva: %s", sorted(body))
        for linha in _mudancas_segredos(antes, sec):
            log.info(linha)
        await rt.reload()
        out = estado(rt)
        out["restart_required"] = cfg["web_port"] != rt.cfg["web_port"]
        out["kick"] = kick
        return web.json_response(out)

    @r.post("/api/livepix/testar")
    async def testar_livepix(req):
        if not seguro(req):
            return web.json_response({"erro": "Origem não permitida."}, status=403)
        try:
            b = await req.json()
        except Exception:
            return web.json_response({"erro": "JSON inválido."}, status=400)
        b = b if isinstance(b, dict) else {}
        sv = settings.load_secrets()["livepix"]
        cid = str(b.get("client_id") or "").strip() or sv["client_id"]
        cs = str(b.get("client_secret") or "").strip() or sv["client_secret"]
        if not cid or not cs:
            return web.json_response({"erro": "Preencha o Client ID e o Client Secret."}, status=400)
        try:
            n = await livepix.testar(rt.session, cid, cs)
        except livepix.AuthInvalida:
            return web.json_response({"erro": "Credenciais inválidas (confira o Client ID, o Client Secret e a permissão messages:read)."}, status=400)
        except livepix.Limite as e:
            return web.json_response({"erro": f"Limite de requisições do LivePix; tente de novo em {int(e.espera)} s."}, status=429)
        except Exception as e:
            log.warning("teste LivePix falhou: %r", e)
            return web.json_response({"erro": "Não consegui falar com o LivePix. Verifique a internet e tente de novo."}, status=502)
        return web.json_response({"ok": True, "mensagens": n})

    @r.get("/api/audio/saidas")
    async def saidas(req):
        return web.json_response({"saidas": audio.listar_saidas()})

    @r.post("/api/audio/testar")
    async def testar(req):
        if not seguro(req):
            return web.json_response({"erro": "Origem não permitida."}, status=403)
        try:
            b = await req.json()
            dev = b.get("device", "") if isinstance(b, dict) else ""
        except Exception:
            return web.json_response({"erro": "JSON inválido."}, status=400)
        if not isinstance(dev, str) or len(dev) > 200:
            return web.json_response({"erro": "Dispositivo inválido."}, status=400)
        try:
            import numpy as np
            n = int(44100 * 0.4)
            t = np.arange(n) / 44100
            onda = (np.sin(2 * np.pi * 880 * t) * np.minimum(1, np.minimum(t, 0.4 - t) * 50)).astype("float32")
            await asyncio.get_running_loop().run_in_executor(
                None, audio.tocar_dados, onda.reshape(-1, 1), 44100, 0.3, dev.strip())
        except Exception as e:
            log.warning("teste de som falhou: %r", e)
            return web.json_response({"erro": "Não consegui tocar o som neste dispositivo."}, status=500)
        return web.json_response({"ok": True})

    @r.post("/api/abrir-sons")
    async def abrir_sons(req):
        if not seguro(req):
            return web.json_response({"erro": "Origem não permitida."}, status=403)
        if sys.platform != "win32" or not hasattr(os, "startfile"):
            return web.json_response({"erro": "Disponível só no Windows."}, status=501)
        os.startfile(str(paths.sons_dir()))
        return web.json_response({"ok": True})

    app.add_routes(r)
