"""App-level envelope encryption for secrets at rest -- specifically customer signing secrets.

The whole point: the key that decrypts the secrets must NOT live on the config volume. The
master key comes from the platform secret store (`OFFBOARD_CONFIG_KEY`, injected as an env
secret via `fly secrets` -- never in the image, never in a config file), so an attacker who
reads the volume gets only ciphertext and can't forge identity tokens.

Fernet (AES-128-CBC + HMAC-SHA256, from the `cryptography` lib) is the standard "don't roll
your own" authenticated primitive. `MultiFernet` gives zero-downtime key rotation: list several
keys in `OFFBOARD_CONFIG_KEY` (comma-separated) and it decrypts with any of them but encrypts
with the FIRST, so you rotate by prepending a new key, re-encrypting, then dropping the old one.

Stored values are prefixed `enc:` so the load path can tell an encrypted secret from a legacy
plaintext one and migrate transparently (see onboarding.encrypt_configs).
"""

import os
from typing import Optional

ENC_PREFIX = "enc:"  # marks a value THIS module produced


class ConfigCryptoError(Exception):
    """The master key is missing when it's needed, or a value failed to decrypt."""


def _fernet():
    """Build a (Multi)Fernet from OFFBOARD_CONFIG_KEY, or None if unset. Imported lazily so the
    in-memory/dev path needs neither the env var nor the `cryptography` dependency."""
    raw = os.getenv("OFFBOARD_CONFIG_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        return None
    from cryptography.fernet import Fernet, MultiFernet

    return MultiFernet([Fernet(k.encode()) for k in keys])


def key_configured() -> bool:
    """True when a master key is present, so callers can encrypt (and warn when they can't)."""
    return _fernet() is not None


def is_encrypted(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(ENC_PREFIX)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage. Raises if no master key is configured -- callers decide
    whether to hard-fail or fall back to writing plaintext (dev), never silently pretend."""
    f = _fernet()
    if f is None:
        raise ConfigCryptoError(
            "OFFBOARD_CONFIG_KEY is not set; cannot encrypt a signing secret at rest"
        )
    return ENC_PREFIX + f.encrypt(plaintext.encode()).decode()


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    """Return the plaintext secret. A value without the `enc:` prefix is treated as legacy
    plaintext and passed through unchanged (so unmigrated files keep working). An encrypted
    value with no/ wrong key is a hard error -- failing closed beats booting a tenant whose
    identity verification would silently accept forged tokens."""
    if value is None:
        return None
    if not value.startswith(ENC_PREFIX):
        return value  # legacy plaintext -- pre-encryption, or a dev file
    f = _fernet()
    if f is None:
        raise ConfigCryptoError(
            "a stored secret is encrypted but OFFBOARD_CONFIG_KEY is not set"
        )
    from cryptography.fernet import InvalidToken

    try:
        return f.decrypt(value[len(ENC_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise ConfigCryptoError(
            "signing secret failed to decrypt -- wrong or rotated-out OFFBOARD_CONFIG_KEY"
        ) from exc


def generate_key() -> str:
    """Mint a fresh master key (for `fly secrets set OFFBOARD_CONFIG_KEY=...`)."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
