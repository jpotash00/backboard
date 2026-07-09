"""Secret-at-rest tests: signing secrets are encrypted with a master key that lives in the
env, never on the config volume. Skips cleanly if `cryptography` isn't installed."""

import json

import pytest

pytest.importorskip("cryptography")

from api import crypto
from api.config_store import load_customer_file
from onboarding.provision import build_record, write_customer_file

SPEC = {
    "customer_id": "acme",
    "public_key": "pk_live_acme",
    "product": {
        "product_name": "Acme",
        "product_context": "does things for teams",
        "activation_definition": "created a project",
        "pricing_summary": "Starter $49, Growth $199",
    },
    "offers": [{"type": "discount", "description": "50% off for 3 months"}],
}


@pytest.fixture
def key(monkeypatch):
    k = crypto.generate_key()
    monkeypatch.setenv("OFFBOARD_CONFIG_KEY", k)
    return k


def test_roundtrip(key):
    token = crypto.encrypt_secret("s3cret")
    assert token.startswith("enc:") and "s3cret" not in token
    assert crypto.decrypt_secret(token) == "s3cret"


def test_legacy_plaintext_passes_through():
    # A value without the enc: prefix is pre-migration plaintext -- returned unchanged.
    assert crypto.decrypt_secret("raw_plaintext_secret") == "raw_plaintext_secret"


def test_encrypt_requires_key(monkeypatch):
    monkeypatch.delenv("OFFBOARD_CONFIG_KEY", raising=False)
    with pytest.raises(crypto.ConfigCryptoError):
        crypto.encrypt_secret("x")


def test_encrypted_value_without_key_fails_closed(key):
    token = crypto.encrypt_secret("s3cret")
    import os
    os.environ.pop("OFFBOARD_CONFIG_KEY")
    with pytest.raises(crypto.ConfigCryptoError):
        crypto.decrypt_secret(token)


def test_wrong_key_fails_closed(monkeypatch):
    monkeypatch.setenv("OFFBOARD_CONFIG_KEY", crypto.generate_key())
    token = crypto.encrypt_secret("s3cret")
    monkeypatch.setenv("OFFBOARD_CONFIG_KEY", crypto.generate_key())  # different key
    with pytest.raises(crypto.ConfigCryptoError):
        crypto.decrypt_secret(token)


def test_rotation_decrypts_with_old_key_encrypts_with_new(monkeypatch):
    old = crypto.generate_key()
    monkeypatch.setenv("OFFBOARD_CONFIG_KEY", old)
    token_old = crypto.encrypt_secret("s3cret")
    new = crypto.generate_key()
    monkeypatch.setenv("OFFBOARD_CONFIG_KEY", f"{new},{old}")  # new primary, old still trusted
    assert crypto.decrypt_secret(token_old) == "s3cret"        # old ciphertext still readable
    reencrypted = crypto.encrypt_secret("s3cret")              # now written under the new key
    monkeypatch.setenv("OFFBOARD_CONFIG_KEY", new)             # drop the old key entirely
    assert crypto.decrypt_secret(reencrypted) == "s3cret"


def test_written_file_holds_ciphertext_but_record_reveals_plaintext(key, tmp_path):
    """The one-time reveal (returned record) is plaintext; the on-disk copy is ciphertext; and
    the load path decrypts back to plaintext so verify_identity is unaffected."""
    record = build_record(SPEC)
    plaintext = record["signing_secret"]
    assert not plaintext.startswith("enc:")          # revealed once, in the clear

    path = write_customer_file(record, str(tmp_path))
    on_disk = json.loads(path.read_text())["signing_secret"]
    assert on_disk.startswith("enc:") and plaintext not in on_disk  # at rest: encrypted

    customer = load_customer_file(path)
    assert customer.signing_secret == plaintext       # decrypted transparently on load
    assert oct(path.stat().st_mode)[-3:] == "600"     # locked down


def test_write_without_key_falls_back_to_plaintext(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("OFFBOARD_CONFIG_KEY", raising=False)
    record = build_record(SPEC)
    path = write_customer_file(record, str(tmp_path))
    on_disk = json.loads(path.read_text())["signing_secret"]
    assert on_disk == record["signing_secret"]         # plaintext (dev)
    assert "PLAINTEXT" in capsys.readouterr().out       # ...but loudly warned
