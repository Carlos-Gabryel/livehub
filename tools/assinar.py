"""Uso: python tools/assinar.py <zip> <versao>  -> grava <zip>.sig (base64 da assinatura Ed25519 do manifesto)."""
import base64, hashlib, os, sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization

DEFAULT = Path.home() / ".livehub-keys" / "release_ed25519.pem"


def assinar(zip_path, versao, key_path=None) -> Path:
    kp = Path(key_path or os.environ.get("LIVEHUB_SIGN_KEY") or DEFAULT)
    if not kp.is_file():
        raise SystemExit(f"Chave privada nao encontrada: {kp} (rode tools/gerar_chave.py ou defina LIVEHUB_SIGN_KEY)")
    priv = serialization.load_pem_private_key(kp.read_bytes(), password=None)
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    sig = priv.sign(f"livehub|{versao}|{h.hexdigest()}".encode())
    out = Path(str(zip_path) + ".sig")
    out.write_bytes(base64.b64encode(sig) + b"\n")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    print(assinar(sys.argv[1], sys.argv[2]))
