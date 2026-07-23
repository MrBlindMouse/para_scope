"""Password hashing, CSRF tokens, signed sessions, and secret encryption."""
import base64
import hashlib
import os
import secrets

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_AES_PREFIX = "aes:"
_XOR_PREFIX = "xor:"  # explicit tag for legacy XOR blobs written after this change
_SESSION_SALT = "para-scope-session"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60  # 14 days
_SESSION_MAX_AGE_SECONDS = SESSION_MAX_AGE_SECONDS  # alias for internal use


def _secret_key() -> str:
    return os.environ.get("PARA_SCOPE_SECRET_KEY", "")


def _fernet():
    """Build a Fernet instance from PARA_SCOPE_SECRET_KEY, or None if unset/unavailable."""
    key = _secret_key()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    # Fernet needs a 32-byte url-safe base64 key
    fernet_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
    return Fernet(fernet_key)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def generate_csrf_token() -> str:
    return secrets.token_hex(32)


def create_session_token(username: str) -> str:
    """Sign a timed session token. Requires PARA_SCOPE_SECRET_KEY."""
    key = _secret_key()
    if not key:
        raise ValueError("PARA_SCOPE_SECRET_KEY is required for sessions")
    serializer = URLSafeTimedSerializer(key, salt=_SESSION_SALT)
    return serializer.dumps({"username": username})


def verify_session_token(token: str) -> str | None:
    """Return username if token is valid and unexpired, else None."""
    if not token:
        return None
    key = _secret_key()
    if not key:
        return None
    serializer = URLSafeTimedSerializer(key, salt=_SESSION_SALT)
    try:
        data = serializer.loads(token, max_age=_SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    username = data.get("username")
    return username if isinstance(username, str) and username else None


def _xor_encrypt(value: str) -> str:
    key = _secret_key()
    key_bytes = hashlib.sha256(key.encode()).digest()
    result = bytearray(value.encode())
    for i, b in enumerate(result):
        result[i] ^= key_bytes[i % len(key_bytes)]
    return base64.b64encode(bytes(result)).decode()


def _xor_decrypt(encrypted: str) -> str:
    key = _secret_key()
    data = base64.b64decode(encrypted.encode())
    key_bytes = hashlib.sha256(key.encode()).digest()
    result = bytearray(data)
    for i, b in enumerate(result):
        result[i] ^= key_bytes[i % len(key_bytes)]
    return bytes(result).decode()


def encrypt_secret(value: str) -> str:
    """Encrypt a secret. Requires PARA_SCOPE_SECRET_KEY; prefers AES (Fernet)."""
    if not _secret_key():
        raise ValueError("PARA_SCOPE_SECRET_KEY is required to store secrets")
    f = _fernet()
    if f is not None:
        token = f.encrypt(value.encode()).decode()
        return _AES_PREFIX + token
    return _XOR_PREFIX + _xor_encrypt(value)


def decrypt_secret(encrypted: str) -> str:
    """Decrypt a secret. Supports AES, tagged XOR, and legacy untagged XOR/base64."""
    if encrypted.startswith(_AES_PREFIX):
        f = _fernet()
        if f is None:
            raise ValueError("AES-encrypted secret requires cryptography + PARA_SCOPE_SECRET_KEY")
        return f.decrypt(encrypted[len(_AES_PREFIX):].encode()).decode()

    if encrypted.startswith(_XOR_PREFIX):
        if not _secret_key():
            raise ValueError("XOR-encrypted secret requires PARA_SCOPE_SECRET_KEY")
        return _xor_decrypt(encrypted[len(_XOR_PREFIX):])

    # Legacy untagged blobs
    data = base64.b64decode(encrypted.encode())
    if not _secret_key():
        return data.decode()
    # Try XOR first (previous default when key was set)
    try:
        return _xor_decrypt(encrypted)
    except Exception:
        return data.decode()
