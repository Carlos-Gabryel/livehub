"""Logica de inicializacao sem GUI: log, porta, instancia unica, iniciar com o Windows, caixas de mensagem."""
import ctypes, json, logging, logging.handlers, socket, sys

from . import paths

MUTEX_NAME = r"Local\LiveHub-singleton"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "LiveHub"
PORTA_PADRAO = 8782
PORTAS_ALTERNATIVAS = range(8783, 8800)
ERROR_ALREADY_EXISTS = 183


def force_utf8():
    for f in (sys.stdout, sys.stderr):
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def setup_logging():
    """Arquivo sempre; stderr so se existir (exe sem console tem sys.stderr = None)."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    for h in list(raiz.handlers):
        raiz.removeHandler(h)
    handlers = [logging.handlers.RotatingFileHandler(
        paths.log_file(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(fmt)
        raiz.addHandler(h)


# ---- porta ----
def porta_livre(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def escolher_porta(preferida, livre=porta_livre, alternativas=PORTAS_ALTERNATIVAS, primeira_vez=False):
    """Porta a usar. Livre -> a preferida. Ocupada: so na primeira execucao procura outra; senao None."""
    if livre(preferida):
        return preferida
    if not primeira_vez:
        return None
    for p in alternativas:
        if livre(p):
            return p
    return None


def porta_configurada() -> int:
    try:
        d = json.loads((paths.data_dir() / "config.json").read_text(encoding="utf-8"))
        return int(d.get("web_port", PORTA_PADRAO))
    except (OSError, ValueError, TypeError, AttributeError):
        return PORTA_PADRAO


# ---- instancia unica ----
def acquire_singleton(kernel32=None):
    """Retorna handle do mutex (guarde-o vivo) ou None se ja ha outra instancia."""
    k = kernel32 or ctypes.windll.kernel32
    k.CreateMutexW.restype = ctypes.c_void_p
    handle = k.CreateMutexW(None, False, MUTEX_NAME)
    if k.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle or 1


# ---- iniciar com o Windows ----
def autostart_enabled(winreg=None) -> bool:
    if winreg is None:
        import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool, exe=None, winreg=None):
    if winreg is None:
        import winreg
    exe = exe or sys.executable
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe}"')
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


def msgbox(texto: str, titulo: str = "LiveHub"):
    try:
        ctypes.windll.user32.MessageBoxW(None, texto, titulo, 0x10)  # MB_ICONERROR
    except Exception:
        pass
