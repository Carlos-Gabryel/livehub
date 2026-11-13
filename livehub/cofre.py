"""Protecao dos segredos com DPAPI do Windows (so o mesmo usuario, no mesmo PC, consegue abrir)."""
import ctypes, sys
from ctypes import wintypes

_ENTROPIA = b"LiveHub-segredos-v1"
_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt32():
    if sys.platform != "win32":
        return None
    try:
        return ctypes.WinDLL("crypt32", use_last_error=True)
    except OSError:
        return None


def disponivel() -> bool:
    return _crypt32() is not None


def _blob(b: bytes):
    buf = ctypes.create_string_buffer(b, len(b))
    return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _chamar(nome: str, dados: bytes) -> bytes:
    dll = _crypt32()
    if dll is None:
        raise OSError("DPAPI indisponivel")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    entrada, b1 = _blob(dados)
    ent, b2 = _blob(_ENTROPIA)
    saida = _Blob()
    if nome == "proteger":
        ok = dll.CryptProtectData(ctypes.byref(entrada), "LiveHub", ctypes.byref(ent), None, None,
                                  _UI_FORBIDDEN, ctypes.byref(saida))
    else:
        ok = dll.CryptUnprotectData(ctypes.byref(entrada), None, ctypes.byref(ent), None, None,
                                    _UI_FORBIDDEN, ctypes.byref(saida))
    if not ok:
        raise OSError(f"DPAPI falhou (erro {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(saida.pbData, saida.cbData)
    finally:
        k32.LocalFree(ctypes.cast(saida.pbData, ctypes.c_void_p))


def proteger(dados: bytes) -> bytes:
    return _chamar("proteger", dados)


def abrir(dados: bytes) -> bytes:
    return _chamar("abrir", dados)
