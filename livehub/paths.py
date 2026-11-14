"""Caminhos: recursos empacotados (somente leitura) e dados do usuario (gravaveis)."""
import os, sys
from pathlib import Path


def resource_dir() -> Path:
    """Pasta do pacote `livehub` (dentro do bundle quando congelado pelo PyInstaller)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "livehub"
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """LIVEHUB_DATA ou %APPDATA%\LiveHub; criada se nao existir."""
    env = os.environ.get("LIVEHUB_DATA")
    if env:
        p = Path(env)
    else:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        p = Path(base) / "LiveHub"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sons_dir() -> Path:
    p = data_dir() / "sons"
    p.mkdir(parents=True, exist_ok=True)
    return p


def imagens_dir() -> Path:
    p = data_dir() / "imagens"
    p.mkdir(parents=True, exist_ok=True)
    return p


def videos_dir() -> Path:
    p = data_dir() / "videos"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_file() -> Path:
    return data_dir() / "livehub.log"
