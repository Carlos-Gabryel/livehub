"""Regras puras dos alertas: categoria do evento, numero do valor e textos."""
import re

CATEGORIAS = ("follow", "sub", "presente", "raid", "cheer", "doacao")
_MAPA = {
    "follow": "follow",
    "sub": "sub", "assinatura": "sub", "membro": "sub",
    "presente de sub": "presente", "presente de membro": "presente",
    "raid": "raid", "host": "raid",
    "bits": "cheer", "kicks": "cheer", "presente": "cheer", "super sticker": "cheer",
    "doação": "doacao", "super chat": "doacao",
}
_COM_MENSAGEM = {"bits", "kicks", "super chat", "doação"}
_NUM_RE = re.compile(r"\d[\d.,]*")


def categoria(ev) -> str | None:
    return _MAPA.get((ev.get("tipo") or "").strip().lower())


def numero(valor) -> float | None:
    """Primeiro numero do texto ('1.234,56', '10,00', '$5.50', '500'); a moeda e ignorada."""
    m = _NUM_RE.search(valor or "")
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if not s:
        return None
    pt, pp = s.rfind(","), s.rfind(".")
    if pt >= 0 and pp >= 0:  # o ultimo separador e o decimal
        dec = "," if pt > pp else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    else:
        sep = "," if pt >= 0 else "." if pp >= 0 else ""
        if sep:
            partes = s.split(sep)
            if len(partes) > 2 or (len(partes[1]) == 3 and partes[0] not in ("0", "")):
                s = "".join(partes)  # separador de milhar
            else:
                s = ".".join(partes)
    try:
        return float(s)
    except ValueError:
        return None


def mensagem(ev) -> str:
    return (ev.get("detalhe") or "") if (ev.get("tipo") or "").strip().lower() in _COM_MENSAGEM else ""


def texto(template, ev) -> str:
    rep = {"{user}": ev.get("user") or "", "{valor}": ev.get("valor") or "", "{detalhe}": ev.get("detalhe") or "",
           "{mensagem}": mensagem(ev), "{plataforma}": ev.get("platform") or ""}
    out = template or ""
    for k, v in rep.items():
        out = out.replace(k, v)
    return re.sub(r"\s+", " ", out).strip()
