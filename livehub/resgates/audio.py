"""Reproducao de audio em processo (sounddevice + soundfile). Saida WASAPI por nome; "" = padrao do sistema."""
import logging

try:
    import sounddevice as sd
    import soundfile as sf
except Exception:  # PortAudio/libsndfile ausentes: app segue sem som
    sd = sf = None

log = logging.getLogger("audio")


def _wasapi_hostapis():
    return [i for i, h in enumerate(sd.query_hostapis()) if "WASAPI" in h["name"]]


def listar_saidas() -> list[str]:
    """Nomes das saidas WASAPI (sem repeticao); se nao houver WASAPI, todas as saidas."""
    if sd is None:
        return []
    wasapi = _wasapi_hostapis()
    nomes = []
    for d in sd.query_devices():
        if d["max_output_channels"] > 0 and (not wasapi or d["hostapi"] in wasapi) and d["name"] not in nomes:
            nomes.append(d["name"])
    return nomes


def _achar_saida(nome: str):
    wasapi = _wasapi_hostapis()
    alvo = nome.lower()
    candidatos = [(i, d["name"].lower()) for i, d in enumerate(sd.query_devices())
                  if d["max_output_channels"] > 0 and (not wasapi or d["hostapi"] in wasapi)]
    for i, n in candidatos:
        if n == alvo:
            return i
    for i, n in candidatos:
        if alvo in n:
            return i
    return None


def tocar(caminho, volume: float = 1.0, device_name: str = ""):
    """Toca o arquivo ate o fim (bloqueante). volume 0..1. Saida nao encontrada -> padrao do sistema."""
    if sd is None or sf is None:
        raise RuntimeError("sounddevice/soundfile indisponiveis")
    dados, taxa = sf.read(str(caminho), dtype="float32", always_2d=True)
    if dados.shape[1] == 1:
        dados = dados.repeat(2, axis=1)
    kw = {}
    if device_name:
        idx = _achar_saida(device_name)
        if idx is None:
            log.warning("Saida de audio '%s' nao encontrada; usando a padrao do sistema", device_name)
        else:
            kw["device"] = idx
            if hasattr(sd, "WasapiSettings") and sd.query_devices(idx)["hostapi"] in _wasapi_hostapis():
                kw["extra_settings"] = sd.WasapiSettings(auto_convert=True)
    sd.play(dados * volume, taxa, **kw)
    sd.wait()


def parar():
    if sd is not None:
        try:
            sd.stop()
        except Exception as e:
            log.warning("falha ao parar o audio: %s", e)
