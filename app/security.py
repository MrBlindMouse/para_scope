"""Password hashing, CSRF tokens, signed sessions, and secret encryption."""
import base64
import hashlib
import os
import secrets

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_AES_PREFIX = "aes:"
_SESSION_SALT = "para-scope-session"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60  # 14 days
_SESSION_MAX_AGE_SECONDS = SESSION_MAX_AGE_SECONDS  # alias for internal use


def _secret_key() -> str:
    return os.environ.get("PARA_SCOPE_SECRET_KEY", "")


def _fernet():
    """Build a Fernet instance from PARA_SCOPE_SECRET_KEY, or None if unset."""
    key = _secret_key()
    if not key:
        return None
    from cryptography.fernet import Fernet
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


def encrypt_secret(value: str) -> str:
    """Encrypt a secret with Fernet. Requires PARA_SCOPE_SECRET_KEY + cryptography."""
    if not _secret_key():
        raise ValueError("PARA_SCOPE_SECRET_KEY is required to store secrets")
    f = _fernet()
    if f is None:
        raise ValueError("cryptography is required to store secrets")
    token = f.encrypt(value.encode()).decode()
    return _AES_PREFIX + token


def decrypt_secret(encrypted: str) -> str:
    """Decrypt a Fernet secret (aes: prefix)."""
    if not encrypted.startswith(_AES_PREFIX):
        raise ValueError("Unrecognized secret encoding (expected aes:)")
    f = _fernet()
    if f is None:
        raise ValueError("AES-encrypted secret requires cryptography + PARA_SCOPE_SECRET_KEY")
    return f.decrypt(encrypted[len(_AES_PREFIX):].encode()).decode()
