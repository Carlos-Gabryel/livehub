"""config.json, secrets.json e mapeamento.json em data_dir(). Escrita atomica, sempre UTF-8."""
import copy, json, logging, os, tempfile
from pathlib import Path

from . import paths

log = logging.getLogger("settings")

DEFAULT_CONFIG = {
    "twitch": {"channel": ""},
    "kick": {"channel": "", "chatroom_id": None, "channel_id": None},
    "youtube": {"handle": "", "check_interval": 60},
    "tiktok": {"username": "", "check_interval": 60},
    "streamerbot_compat": {"enabled": False, "port": 8080},
    "web_port": 8782,
    "audio": {"output_device": ""},
}
DEFAULT_SECRETS = {
    "twitch": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "",
               "expires_at": 0, "user_id": "", "login": ""},
    "streamelements": {"jwt": ""},
}


def deep_merge(base: dict, over: dict) -> dict:
    """Copia de `base` com `over` por cima (recursivo em dicts)."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def atomic_write_json(path: Path, data, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        if mode:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path):
    """dict lido, ou None se o arquivo nao existe / esta ilegivel."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("raiz nao e um objeto")
        return d
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as e:
        log.warning("%s ilegivel (%s); usando padroes", path.name, e)
        try:
            os.replace(path, path.with_name(path.name + ".bad"))
        except OSError:
            pass
        return None


def config_path() -> Path:
    return paths.data_dir() / "config.json"


def secrets_path() -> Path:
    return paths.data_dir() / "secrets.json"


def mapeamento_path() -> Path:
    return paths.data_dir() / "mapeamento.json"


def _load_with_defaults(path: Path, defaults: dict, mode=None) -> dict:
    d = _read_json(path)
    merged = deep_merge(defaults, d or {})
    if d is None:
        atomic_write_json(path, merged, mode)
    return merged


def load_config() -> dict:
    return _load_with_defaults(config_path(), DEFAULT_CONFIG)


def save_config(cfg: dict):
    atomic_write_json(config_path(), deep_merge(DEFAULT_CONFIG, cfg))


def load_secrets() -> dict:
    return _load_with_defaults(secrets_path(), DEFAULT_SECRETS, 0o600)


def save_secrets(sec: dict):
    atomic_write_json(secrets_path(), deep_merge(DEFAULT_SECRETS, sec), 0o600)


def load_mapeamento() -> dict:
    return _read_json(mapeamento_path()) or {}


def save_mapeamento(m: dict):
    atomic_write_json(mapeamento_path(), m)


def runtime_config() -> dict:
    """config.json + credenciais necessarias aos modulos de chat (twitch client_id/secret, SE jwt)."""
    cfg, sec = load_config(), load_secrets()
    cfg["twitch"] = {**cfg["twitch"], "client_id": sec["twitch"]["client_id"],
                     "client_secret": sec["twitch"]["client_secret"]}
    cfg["streamelements"] = {"jwt": sec["streamelements"]["jwt"]}
    return cfg
