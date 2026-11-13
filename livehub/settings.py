"""config.json, secrets.json e mapeamento.json em data_dir(). Escrita atomica, sempre UTF-8."""
import copy, json, logging, os, tempfile
from pathlib import Path

from . import paths
from .resgates import const

log = logging.getLogger("settings")

_APAR_BASE = {
    "fonte": "system-ui", "tamanho": 14, "negrito": False,
    "cor_fundo": "#0e0e10", "cor_fundo_fim": "", "opacidade_fundo": 100,
    "cor_msg_fundo": "#000000", "opacidade_msg": 0,
    "cor_texto": "#efeff1", "cor_destaque": "#1f1a2b",
    "cor_nome_modo": "usuario", "cor_nome_fixa": "#d0d0d6",
    "contorno": "nenhum", "cor_contorno": "#000000",
    "espacamento": 3, "raio": 0,
    "mostrar_icones": True, "mostrar_badges": True, "mostrar_emotes": True,
    "animacao": "nenhuma", "sumir": 0, "max": 300,
}
# Padroes = visual original de cada pagina (nada muda para quem ja usa).
DEFAULT_APARENCIA = {
    "dock": dict(_APAR_BASE),
    "neutro": {**_APAR_BASE, "fonte": "Fira Code", "tamanho": 22, "negrito": True,
               "cor_fundo": "#dcebff", "cor_fundo_fim": "#ebe1ff", "cor_texto": "#1e1b3a",
               "cor_nome_modo": "paleta", "cor_nome_fixa": "#1e1b3a", "espacamento": 4,
               "mostrar_icones": False, "mostrar_badges": False, "max": 15},
}
DEFAULT_ALERTAS = {
    "ativo": True,
    "estilo": {"fonte": "system-ui", "tamanho": 42, "cor_texto": "#ffffff", "cor_destaque": "#a970ff",
               "contorno": True, "animacao": "pop", "imagem_largura": 300},
    "categorias": {
        "follow": {"ativo": True, "texto": "{user} seguiu!", "som": "", "volume": 80, "imagem": "", "duracao": 5, "minimo": 0, "tts": False},
        "sub": {"ativo": True, "texto": "{user} assinou! {detalhe}", "som": "", "volume": 80, "imagem": "", "duracao": 7, "minimo": 0, "tts": False},
        "presente": {"ativo": True, "texto": "{user} presenteou {valor}!", "som": "", "volume": 80, "imagem": "", "duracao": 7, "minimo": 0, "tts": False},
        "raid": {"ativo": True, "texto": "{user} chegou com {valor}!", "som": "", "volume": 80, "imagem": "", "duracao": 8, "minimo": 0, "tts": False},
        "cheer": {"ativo": True, "texto": "{user} mandou {valor}!", "som": "", "volume": 80, "imagem": "", "duracao": 7, "minimo": 0, "tts": False},
        "doacao": {"ativo": True, "texto": "{user} doou {valor}!", "som": "", "volume": 80, "imagem": "", "duracao": 8, "minimo": 0, "tts": False},  # TTS opt-in: trolls pagam pouco para a live falar ofensa
    },
    "tts": {"motor": "neural", "voz_neural": "pt-BR-FranciscaNeural", "voz_windows": "", "volume": 90,
            "minimo": 0, "max_caracteres": 200, "ler_nome": True, "remover_links": True, "palavras_bloqueadas": []},
}
DEFAULT_CONFIG = {
    "twitch": {"channel": ""},
    "kick": {"channel": "", "chatroom_id": None, "channel_id": None},
    "youtube": {"handle": "", "check_interval": 60},
    "tiktok": {"username": "", "check_interval": 60},
    "streamerbot_compat": {"enabled": False, "port": 8080},
    "web_port": 8782,
    "audio": {"output_device": ""},
    "aparencia": DEFAULT_APARENCIA,
    "atualizacao": {"verificar": True, "modo": "avisar"},  # modo: "avisar" | "ao_sair"
    "alertas": DEFAULT_ALERTAS,
    "settings_version": 1,
}
DEFAULT_SECRETS = {
    "twitch": {"client_id": "", "client_secret": "", "access_token": "", "refresh_token": "",
               "expires_at": 0, "user_id": "", "login": "", "login_expirado": False},
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


# Migracoes do config.json. Para mudar o formato no futuro: suba "settings_version" em DEFAULT_CONFIG
# para N e registre aqui MIGRACOES[N] = fn(cfg) -> cfg (transforma o formato N-1 em N).
# Rodam em ordem crescente, uma vez, so para arquivos com versao menor (sem a chave = 0).
MIGRACOES: dict = {}


def migrar(cfg: dict, v0: int) -> dict:
    for n in sorted(k for k in MIGRACOES if k > v0):
        cfg = MIGRACOES[n](cfg)
    cfg["settings_version"] = max([DEFAULT_CONFIG["settings_version"], *MIGRACOES])
    return cfg


def load_config() -> dict:
    path = config_path()
    bruto = _read_json(path)
    cfg = _load_with_defaults(path, DEFAULT_CONFIG)
    if bruto is None:  # arquivo novo ja nasce na versao atual
        return cfg
    v0 = bruto.get("settings_version", 0)
    v0 = v0 if isinstance(v0, int) else 0
    if v0 < cfg["settings_version"] or any(k > v0 for k in MIGRACOES):
        cfg = migrar(cfg, v0)
        atomic_write_json(path, cfg)
    return cfg


def save_config(cfg: dict):
    atomic_write_json(config_path(), deep_merge(DEFAULT_CONFIG, cfg))


MODOS_TWITCH = ("dispositivo", "app_proprio")


def load_secrets() -> dict:
    sec = _load_with_defaults(secrets_path(), DEFAULT_SECRETS, 0o600)
    t = sec["twitch"]
    if t.get("modo") not in MODOS_TWITCH:  # migracao: quem ja tinha app proprio continua nele
        t["modo"] = "app_proprio" if (t.get("client_id") and t.get("client_secret")) else "dispositivo"
    return sec


def save_secrets(sec: dict):
    atomic_write_json(secrets_path(), deep_merge(DEFAULT_SECRETS, sec), 0o600)


def load_mapeamento() -> dict:
    return _read_json(mapeamento_path()) or {}


def save_mapeamento(m: dict):
    atomic_write_json(mapeamento_path(), m)


def runtime_config() -> dict:
    """config.json + credenciais necessarias aos modulos de chat (twitch client_id/secret, SE jwt)."""
    cfg, sec = load_config(), load_secrets()
    t = sec["twitch"]
    disp = t["modo"] == "dispositivo"
    cfg["twitch"] = {**cfg["twitch"], "modo": t["modo"],
                     "client_id": const.LIVEHUB_CLIENT_ID if disp else t["client_id"],
                     "client_secret": "" if disp else t["client_secret"]}
    cfg["streamelements"] = {"jwt": sec["streamelements"]["jwt"]}
    return cfg
