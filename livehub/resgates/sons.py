"""Sons do usuario: sempre dentro de data_dir()/sons."""
from pathlib import Path

from .. import paths
from . import const


def resolve_som(som: str) -> Path | None:
    """Resolve um caminho de som garantindo que fique dentro da pasta de sons."""
    if not som:
        return None
    root = paths.sons_dir().resolve()
    p = Path(som)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if root in p.parents and p.is_file():
        return p
    return None


def listar_sons() -> list[str]:
    root = paths.sons_dir().resolve()
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.suffix.lower() in const.SOM_EXTS)
