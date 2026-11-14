"""Upload de midia (som/imagem/video) classificado por extensao. Grava em streaming, nome seguro, sem sobrescrever."""
import os, re
from pathlib import Path

from . import paths
from .resgates import const

IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".apng"}
VIDEO_EXTS = {".mp4", ".webm"}
MB = 1024 * 1024
LIMITE_PEQUENO, LIMITE_VIDEO = 50 * MB, 500 * MB
TIPOS = {**{e: ("som", paths.sons_dir, LIMITE_PEQUENO) for e in const.SOM_EXTS},
         **{e: ("imagem", paths.imagens_dir, LIMITE_PEQUENO) for e in IMG_EXTS},
         **{e: ("video", paths.videos_dir, LIMITE_VIDEO) for e in VIDEO_EXTS}}
_PROIBIDOS = re.compile(r'[\x00-\x1f\x7f<>:"/\|?*]')
_RESERVADOS = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def nome_seguro(nome) -> str | None:
    if not isinstance(nome, str):
        return None
    base = nome.replace("\\", "/").rsplit("/", 1)[-1]
    base = _PROIBIDOS.sub("", base).strip(". ")
    stem, ext = os.path.splitext(base)
    stem = stem.strip(". ")[:100].rstrip(". ")
    if not stem or stem.split(".")[0].upper() in _RESERVADOS:
        return None
    return stem + ext


def classificar(nome):
    return TIPOS.get(Path(nome).suffix.lower())


def _livre(pasta: Path, nome: str) -> Path:
    p = pasta / nome
    n = 2
    while p.exists():
        p = pasta / f"{Path(nome).stem} ({n}){Path(nome).suffix}"
        n += 1
    return p


async def salvar(nome, chunks) -> dict:
    """`chunks`: async iterable de bytes. ValueError("tipo"|"nome"|"grande")."""
    seguro = nome_seguro(nome)
    if seguro is None:
        raise ValueError("nome")
    cls = classificar(seguro)
    if cls is None:
        raise ValueError("tipo")
    tipo, dir_fn, limite = cls
    pasta = dir_fn()
    tmp = pasta / f".{seguro}.part"
    try:
        total = 0
        with open(tmp, "wb") as f:
            async for ch in chunks:
                total += len(ch)
                if total > limite:
                    raise ValueError("grande")
                f.write(ch)
        for _ in range(100):  # re-checa o nome livre logo antes de gravar (corrida entre uploads)
            dest = _livre(pasta, seguro)
            if not dest.exists():
                os.replace(tmp, dest)
                return {"tipo": tipo, "nome": dest.name}
        raise ValueError("nome")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
