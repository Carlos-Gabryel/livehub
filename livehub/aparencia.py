"""Aparencia do chat (/dock e /neutro): validacao, CSS gerado e API. Mudancas valem ao vivo, sem reload()."""
import copy, json, logging, re

from aiohttp import web

from . import settings
from .configweb import Erro, _inteiro
from .web import seguro

log = logging.getLogger("aparencia")

ALVOS = ("dock", "neutro")
FONTES_SUGERIDAS = ["Fira Code", "Segoe UI", "Arial", "Verdana", "Tahoma", "Trebuchet MS", "Georgia",
                    "Consolas", "Comic Sans MS", "Impact"]
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
FONTE_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,60}$")
ENUMS = {
    "cor_nome_modo": ("usuario", "plataforma", "fixa", "paleta"),
    "contorno": ("nenhum", "sombra", "contorno"),
    "animacao": ("nenhuma", "fade", "deslizar"),
}
CORES = ("cor_fundo", "cor_msg_fundo", "cor_texto", "cor_destaque", "cor_nome_fixa", "cor_contorno")
BOOLS = ("negrito", "mostrar_icones", "mostrar_badges", "mostrar_emotes")
INTS = {"tamanho": (10, 48, "Use um valor entre 10 e 48."),
        "opacidade_fundo": (0, 100, "Use um valor entre 0 e 100."),
        "opacidade_msg": (0, 100, "Use um valor entre 0 e 100."),
        "espacamento": (0, 20, "Use um valor entre 0 e 20."),
        "raio": (0, 24, "Use um valor entre 0 e 24."),
        "sumir": (0, 600, "Use um valor entre 0 e 600 segundos (0 = nunca)."),
        "max": (1, 300, "Use um valor entre 1 e 300.")}


def validar_campo(campo, v):
    if campo in CORES or campo == "cor_fundo_fim":
        if campo == "cor_fundo_fim" and (v is None or v == ""):
            return ""
        if not isinstance(v, str) or not HEX_RE.match(v):
            raise Erro(campo, "Cor inválida (use o formato #RRGGBB).")
        return v.lower()
    if campo in BOOLS:
        if not isinstance(v, bool):
            raise Erro(campo, "Valor inválido (sim ou não).")
        return v
    if campo in INTS:
        lo, hi, msg = INTS[campo]
        return _inteiro(campo, v, lo, hi, msg)
    if campo in ENUMS:
        if not isinstance(v, str) or v not in ENUMS[campo]:
            raise Erro(campo, "Opção inválida.")
        return v
    if campo == "fonte":
        if not isinstance(v, str) or not FONTE_RE.match(v.strip()):
            raise Erro(campo, "Nome de fonte inválido (use só letras, números, espaço, ponto, hífen e sublinhado).")
        return v.strip()
    raise Erro(campo, "Campo desconhecido.")


def validar(valores):
    """Valida um dict parcial; devolve o dict normalizado ou levanta Erro."""
    if not isinstance(valores, dict):
        raise Erro("valores", "Formato inválido.")
    return {k: validar_campo(k, v) for k, v in valores.items()}


def obter(alvo, cfg=None):
    """Valores efetivos do alvo: padroes + config, campo invalido volta ao padrao."""
    cfg = cfg if cfg is not None else settings.load_config()
    out = copy.deepcopy(settings.DEFAULT_APARENCIA[alvo])
    salvo = cfg.get("aparencia", {}).get(alvo, {})
    for k, v in (salvo.items() if isinstance(salvo, dict) else ()):
        if k in out:
            try:
                out[k] = validar_campo(k, v)
            except Erro:
                pass
    return out


def _rgba(hexa, pct):
    h = hexa.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{pct / 100:.2f})"


def gerar_css(v):
    fonte = v["fonte"]
    pilha = f'"{fonte}", system-ui, "Segoe UI", Roboto, sans-serif'
    if fonte in ("Fira Code", "Consolas"):
        pilha = f'"{fonte}", monospace'
    fundo = _rgba(v["cor_fundo"], v["opacidade_fundo"])
    fim = _rgba(v["cor_fundo_fim"], v["opacidade_fundo"]) if v["cor_fundo_fim"] else fundo
    c = v["cor_contorno"]
    if v["contorno"] == "sombra":
        sombra = f"0 1px 3px {c}, 0 0 2px {c}"
    elif v["contorno"] == "contorno":
        sombra = ", ".join(f"{x}px {y}px 0 {c}" for x, y in
                           ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)))
    else:
        sombra = "none"
    tem_bg = v["opacidade_msg"] > 0
    linhas = [
        ":root{",
        f"--fonte:{pilha};--tam:{v['tamanho']}px;--peso:{700 if v['negrito'] else 400};"
        f"--peso-t:{600 if v['negrito'] else 400};",
        f"--fundo:{fundo};--fundo-fim:{fim};--borda-caixa:{_rgba('#c9c4f0', v['opacidade_fundo'])};",
        f"--msg-bg:{_rgba(v['cor_msg_fundo'], v['opacidade_msg'])};--msg-pad:{'2px 6px' if tem_bg else '0'};",
        f"--texto:{v['cor_texto']};--destaque:{v['cor_destaque']};--nome-fixa:{v['cor_nome_fixa']};",
        f"--sombra:{sombra};--esp:{v['espacamento']}px;--raio:{v['raio']}px;",
        "}",
    ]
    nome = {"usuario": "var(--uc)", "plataforma": "var(--c)", "fixa": "var(--nome-fixa)", "paleta": "var(--up)"}
    linhas.append(f".m .u{{color:{nome[v['cor_nome_modo']]}}}")
    if not v["mostrar_icones"]:
        linhas.append(".ic{display:none !important}")
    if not v["mostrar_badges"]:
        linhas.append(".badge{display:none !important}")
    if not v["mostrar_emotes"]:
        linhas.append(".emote{display:none !important}.emote-txt{display:inline !important}")
    if v["animacao"] == "fade":
        linhas.append(".m.novo{animation:ap-fade .35s ease-out}@keyframes ap-fade{from{opacity:0}to{opacity:1}}")
    elif v["animacao"] == "deslizar":
        linhas.append(".m.novo{animation:ap-desliza .35s ease-out}"
                      "@keyframes ap-desliza{from{opacity:0;transform:translateX(-24px)}to{opacity:1;transform:none}}")
    return "\n".join(linhas) + "\n"


def add_routes(app: web.Application, rt):
    r = web.RouteTableDef()

    def alvo_de(req):
        a = req.match_info["alvo"]
        if a not in ALVOS:
            raise web.HTTPNotFound()
        return a

    @r.get("/aparencia/{alvo}.css")
    async def css(req):
        a = alvo_de(req)
        v = obter(a)
        pre = req.query.get("v")  # previa sem salvar (usada pela pagina de configuracoes)
        if pre:
            try:
                v.update(validar(json.loads(pre)))
            except (ValueError, Erro):
                return web.Response(status=400, text="/* previa invalida */", content_type="text/css")
        return web.Response(text=gerar_css(v), content_type="text/css", charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    def estado():
        cfg = settings.load_config()
        return {"dock": obter("dock", cfg), "neutro": obter("neutro", cfg),
                "defaults": copy.deepcopy(settings.DEFAULT_APARENCIA), "fontes_sugeridas": FONTES_SUGERIDAS}

    @r.get("/api/aparencia")
    async def get_aparencia(req):
        return web.json_response(estado(), headers={"Cache-Control": "no-store"})

    async def corpo(req):
        """(body, resposta_de_erro)."""
        if not seguro(req):
            return None, web.json_response({"erro": "Origem não permitida."}, status=403)
        try:
            b = await req.json()
        except Exception:
            b = None
        if not isinstance(b, dict) or b.get("alvo") not in ALVOS:
            return None, web.json_response({"erro": "JSON inválido."}, status=400)
        return b, None

    async def salvar(alvo, novos):
        cfg = settings.load_config()
        cfg["aparencia"][alvo] = novos
        try:
            settings.save_config(cfg)
        except OSError as e:
            log.warning("falha ao salvar aparencia: %r", type(e).__name__)
            return web.json_response({"erro": "Não consegui salvar as configurações."}, status=500)
        await rt.hub.aparencia(alvo, novos)
        return web.json_response(estado())

    @r.post("/api/aparencia")
    async def post_aparencia(req):
        b, err = await corpo(req)
        if err:
            return err
        try:
            mudancas = validar(b.get("valores"))
        except Erro as e:
            return web.json_response({"campo": e.campo, "erro": e.msg}, status=400)
        novos = obter(b["alvo"])
        novos.update(mudancas)
        return await salvar(b["alvo"], novos)

    @r.post("/api/aparencia/reset")
    async def reset(req):
        b, err = await corpo(req)
        if err:
            return err
        return await salvar(b["alvo"], copy.deepcopy(settings.DEFAULT_APARENCIA[b["alvo"]]))

    app.add_routes(r)
