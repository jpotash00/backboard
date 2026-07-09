"""One-shot migration: encrypt the signing_secret in existing customer config files.

Before this fix, provisioned customers stored their signing_secret in plaintext on the config
volume. Set OFFBOARD_CONFIG_KEY (see api.crypto.generate_key) and run this to rewrite each
file with the secret encrypted in place. Idempotent -- already-encrypted files are skipped, so
it's safe to run repeatedly (e.g. after adding a customer with an older tool).

    OFFBOARD_CONFIG_KEY=... python -m onboarding.encrypt_configs [config_dir]

config_dir defaults to $OFFBOARD_CONFIG_DIR. Nothing is decrypted or printed; the plaintext
secret is never recoverable from here (it was shown once at provision time).
"""

import json
import os
import sys
from pathlib import Path


def encrypt_dir(config_dir: str) -> tuple[int, int]:
    """Encrypt every unencrypted signing_secret under config_dir. Returns (encrypted, skipped)."""
    from api.crypto import encrypt_secret, is_encrypted, key_configured

    if not key_configured():
        raise SystemExit(
            "OFFBOARD_CONFIG_KEY is not set. Mint one with "
            "`python -c 'from api.crypto import generate_key; print(generate_key())'`, "
            "store it as a platform secret, then re-run."
        )

    files = sorted(Path(config_dir).glob("*.json"))
    encrypted = skipped = 0
    for path in files:
        data = json.loads(path.read_text())
        secret = data.get("signing_secret")
        if not secret or is_encrypted(secret):
            skipped += 1
            continue
        data["signing_secret"] = encrypt_secret(secret)
        path.write_text(json.dumps(data, indent=2) + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        encrypted += 1
        print(f"encrypted {path.name}")
    return encrypted, skipped


def main() -> int:
    config_dir = sys.argv[1] if len(sys.argv) > 1 else os.getenv("OFFBOARD_CONFIG_DIR")
    if not config_dir:
        print("usage: python -m onboarding.encrypt_configs <config_dir>  (or set OFFBOARD_CONFIG_DIR)")
        return 1
    encrypted, skipped = encrypt_dir(config_dir)
    print(f"done: {encrypted} encrypted, {skipped} already-encrypted/empty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
