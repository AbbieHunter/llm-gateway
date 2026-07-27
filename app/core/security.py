"""Auth primitives (M1).

Password hashing: stdlib `hashlib.pbkdf2_hmac` + `secrets` random salt (R2).
Zero third-party native deps — avoids the bcrypt/C-extension build break on
Python 3.13 that we hit in M0.

Sessions: DB-backed (R1). A signed pyjwt carries `{sub, jti, exp}`; the `jti`
is persisted in the `sessions` table with `revoked=False`. Revocation (logout,
account disable) flips `revoked=True`, which `session_auth` enforces on every
request — so disabling an account invalidates its existing sessions immediately.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import secrets
import time
import uuid

import jwt

from app.config import JWT_SECRET, SESSION_EXPIRE_MIN
from app.db.models import Session
from app.db.session import async_session_factory

_PBKDF2_ALGO = "sha256"
_PBKDF2_ITERS = 100_000
_SEP = "$"


class SecurityError(Exception):
    pass


# --- password hashing (stdlib only) ---

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return _SEP.join([_PBKDF2_ALGO, str(_PBKDF2_ITERS), salt.hex(), dk.hex()])


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_hex, dk_hex = stored.split(_SEP)
    except ValueError:
        return False
    if algo != _PBKDF2_ALGO:
        return False
    dk = hashlib.pbkdf2_hmac(
        algo, password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s)
    )
    return hmac.compare_digest(dk.hex(), dk_hex)


# --- session tokens (pyjwt, pure python) ---

def _encode_token(account_id: str, jti: str, exp: int) -> str:
    payload = {"sub": account_id, "jti": jti, "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    """Return payload dict, or raise SecurityError on invalid/expired."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as exc:  # invalid signature / expired / malformed
        raise SecurityError(str(exc)) from exc


async def create_session(account_id: str) -> tuple[str, str]:
    """Create a DB-backed session row and return (jti, signed_token)."""
    jti = uuid.uuid4().hex
    # exp as epoch seconds (timezone-safe; naive .timestamp() misreads local tz).
    exp = int(time.time()) + SESSION_EXPIRE_MIN * 60
    expires_at = datetime.datetime.fromtimestamp(exp, tz=datetime.timezone.utc)
    async with async_session_factory() as db:
        db.add(Session(jti=jti, account_id=account_id, expires_at=expires_at, revoked=False))
        await db.commit()
    return jti, _encode_token(account_id, jti, exp)


async def revoke_session(jti: str) -> None:
    async with async_session_factory() as db:
        session = await db.get(Session, jti)
        if session:
            session.revoked = True
            await db.commit()


async def revoke_all_for_account(account_id: str) -> None:
    """Revoke every session for an account (used on account disable, R1)."""
    async with async_session_factory() as db:
        from sqlalchemy import select, update

        await db.execute(
            update(Session).where(Session.account_id == account_id).values(revoked=True)
        )
        await db.commit()
