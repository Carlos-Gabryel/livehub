"""Pagina /config e API /api/config: configuracao completa pelo navegador, sem editar arquivos."""
import asyncio, copy, logging, os, re, sys

from aiohttp import web

from . import paths, settings
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
            "recompensas": b + "/recompensas"}


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
        "audio": {"saidas": saidas, "atual": cfg["audio"]["output_device"]},
        "sons_dir": str(paths.sons_dir()),
        "web_port": porta,
        "urls": urls(porta),
    }


def add_routes(app: web.Application, rt):
    app[RT] = rt
    r = web.RouteTableDef()

    @r.get("/config")
    async def pagina(req):
        return web.FileResponse(paths.resource_dir() / "static" / "config.html")

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
        try:
            info = aplicar(body, cfg, sec)
        except Erro as e:
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
        await rt.reload()
        out = estado(rt)
        out["restart_required"] = cfg["web_port"] != rt.cfg["web_port"]
        out["kick"] = kick
        return web.json_response(out)

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
