"""TTS dos alertas: Edge neural (online, mp3) com queda para SAPI do Windows (offline, wav). Nunca levanta."""
import asyncio, logging, os, re, subprocess, sys, uuid
from pathlib import Path

from .. import paths

log = logging.getLogger("tts")

VOZES_NEURAL = ["pt-BR-FranciscaNeural", "pt-BR-AntonioNeural", "pt-BR-ThalitaNeural", "en-US-JennyNeural",
                "en-US-GuyNeural", "es-ES-ElviraNeural"]
VOZ_RE = re.compile(r"^[A-Za-z]{2,3}-[A-Za-z]{2,4}-[A-Za-z]+Neural$")
LINK_RE = re.compile(r"(?:https?://|www\.)\S+|\b\S+\.(?:com|net|org|tv|gg|br|io|me|ly)(?:/\S*)?\b", re.I)
MANTER = 50
TIMEOUT_NEURAL = 15
TIMEOUT_WINDOWS = 20
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PS = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
      "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
# O texto do usuario nunca entra no script: vai por variavel de ambiente.
SCRIPT_FALA = ("$ErrorActionPreference='Stop';Add-Type -AssemblyName System.Speech;"
               "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
               "if($env:LH_VOZ){try{$s.SelectVoice($env:LH_VOZ)}catch{}};"
               "$s.SetOutputToWaveFile($env:LH_SAIDA);$s.Speak($env:LH_TEXTO);$s.Dispose()")
SCRIPT_VOZES = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;Add-Type -AssemblyName System.Speech;"
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                "$s.GetInstalledVoices()|ForEach-Object{$_.VoiceInfo.Name};$s.Dispose()")


def tts_dir() -> Path:
    p = paths.data_dir() / "tts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _filtrar(t, cfg) -> str:
    if cfg.get("remover_links", True):
        t = LINK_RE.sub(" ", t)
    for p in cfg.get("palavras_bloqueadas") or []:
        p = (p or "").strip()
        if p:
            t = re.sub(r"(?<!\w)" + re.escape(p) + r"(?!\w)", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def preparar(texto, cfg, user="") -> str:
    """Limpa o texto conforme a config; '' se nao sobrar nada para falar. O nome passa pelo mesmo filtro."""
    t = _filtrar(texto or "", cfg)
    if not t:
        return ""
    t = t[:int(cfg.get("max_caracteres") or 200)].strip()
    u = _filtrar(user or "", cfg)[:40].strip() if cfg.get("ler_nome", True) else ""
    if u:
        t = f"{u} disse: {t}"
    return t


def _limpar():
    try:
        arqs = sorted((p for p in tts_dir().iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in arqs[MANTER:]:
            p.unlink(missing_ok=True)
    except OSError:
        log.warning("nao consegui limpar a pasta do TTS")


async def _neural(texto, voz, destino: Path):
    import edge_tts
    if not VOZ_RE.match(voz or ""):
        voz = VOZES_NEURAL[0]
    await asyncio.wait_for(edge_tts.Communicate(texto, voz).save(str(destino)), TIMEOUT_NEURAL)


async def _powershell(script, env_extra, timeout):
    env = {**os.environ, **env_extra}
    kw = {"creationflags": _NOWIN} if sys.platform == "win32" else {}
    proc = await asyncio.create_subprocess_exec(*PS, script, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                env=env, cwd=str(paths.data_dir()), **kw)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    return proc.returncode, out, err


async def _windows(texto, voz, destino: Path):
    rc, _, err = await _powershell(SCRIPT_FALA, {"LH_TEXTO": texto, "LH_VOZ": voz or "", "LH_SAIDA": str(destino)},
                                   TIMEOUT_WINDOWS)
    if rc != 0:
        raise RuntimeError("SAPI falhou: " + (err or b"").decode("utf-8", "replace")[:200])


async def vozes_windows() -> list[str]:
    try:
        rc, out, _ = await _powershell(SCRIPT_VOZES, {}, TIMEOUT_WINDOWS)
        if rc != 0:
            return []
        return [l.strip() for l in out.decode("utf-8", "replace").splitlines() if l.strip()]
    except Exception as e:  # noqa: BLE001
        log.warning("listar vozes do Windows falhou: %r", e)
        return []


async def gerar(texto, cfg, user="") -> Path | None:
    """Gera o audio e devolve o caminho (em data_dir()/tts), ou None."""
    try:
        t = preparar(texto, cfg, user)
        if not t:
            return None
        base = tts_dir() / uuid.uuid4().hex
        if cfg.get("motor", "neural") == "neural":
            dest = base.with_suffix(".mp3")
            try:
                await _neural(t, cfg.get("voz_neural", ""), dest)
                if dest.is_file() and dest.stat().st_size > 0:
                    _limpar()
                    return dest
                raise RuntimeError("audio vazio")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("TTS neural falhou (%r); usando voz do Windows", e)
                dest.unlink(missing_ok=True)
        dest = base.with_suffix(".wav")
        await _windows(t, cfg.get("voz_windows", ""), dest)
        if dest.is_file() and dest.stat().st_size > 0:
            _limpar()
            return dest
        raise RuntimeError("audio vazio")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("TTS falhou: %r", e)
        return None
