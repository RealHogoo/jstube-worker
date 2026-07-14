import base64
import hashlib
import hmac
import secrets

from django.conf import settings


def encrypt_job_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    key = _job_secret_key()
    nonce = secrets.token_bytes(16)
    payload = text.encode("utf-8")
    cipher = _xor_stream(payload, key, nonce)
    mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + mac + cipher).decode("ascii")


def decrypt_job_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    raw = base64.urlsafe_b64decode(text.encode("ascii"))
    if len(raw) < 48:
        return ""
    nonce = raw[:16]
    expected_mac = raw[16:48]
    cipher = raw[48:]
    key = _job_secret_key()
    actual_mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_mac, actual_mac):
        return ""
    return _xor_stream(cipher, key, nonce).decode("utf-8")


def _job_secret_key() -> bytes:
    secret = str(getattr(settings, "SECRET_KEY", "") or "")
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _xor_stream(payload: bytes, key: bytes, nonce: bytes) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < len(payload):
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        output.extend(block)
        counter += 1
    return bytes(byte ^ stream for byte, stream in zip(payload, output))
