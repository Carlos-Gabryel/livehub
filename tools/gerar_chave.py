"""Gera o par Ed25519 de assinatura de releases em ~/.livehub-keys/release_ed25519.pem (nunca sobrescreve)."""
import base64, sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT = Path.home() / ".livehub-keys" / "release_ed25519.pem"


def pub_b64(priv) -> str:
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def main() -> int:
    if DEFAULT.exists():
        print(f"RECUSADO: {DEFAULT} ja existe (nao sobrescrevo).", file=sys.stderr)
        return 1
    DEFAULT.parent.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    with open(DEFAULT, "xb") as f:
        f.write(pem)
    print(f"Chave privada gravada em {DEFAULT} (faca backup offline; nunca commite).")
    print("CHAVE_PUBLICA =", pub_b64(priv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
