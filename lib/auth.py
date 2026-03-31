"""
Authentication helpers: password hashing, JWT, and route decorators.

Learning notes:
- Passwords are hashed with bcrypt (one-way, slow by design — hard to brute-force)
- JWTs are SIGNED, not encrypted — the payload is base64, not secret
- httpOnly cookies can't be read by JavaScript — protects against XSS token theft
- Stateless JWT means no server-side session store needed (good for serverless)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from functools import wraps

import bcrypt
import jwt
from flask import request, jsonify, g


def hash_password(plain: str) -> str:
    """Hash a plain-text password. rounds=12 is intentionally slow."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the stored bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_token(user_id: int, email: str, is_admin: bool) -> str:
    """Issue a signed JWT. The payload is readable but tamper-proof."""
    payload = {
        "sub": user_id,       # subject = user's DB id
        "email": email,
        "is_admin": is_admin,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=30),
    }
    return jwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")


def decode_token(token: str) -> dict:
    """Verify signature and expiry. Raises jwt.InvalidTokenError on failure."""
    return jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"])


def _extract_user() -> dict:
    """Read and verify the JWT cookie. Raises ValueError if missing/invalid."""
    token = request.cookies.get("tv_token")
    if not token:
        raise ValueError("missing token")
    return decode_token(token)


def require_auth(f):
    """Decorator: 401 if not logged in. Sets g.current_user on success."""
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            g.current_user = _extract_user()
        except Exception:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """Decorator: 401 if not logged in, 403 if not admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            g.current_user = _extract_user()
        except Exception:
            return jsonify({"error": "Authentication required"}), 401
        if not g.current_user.get("is_admin"):
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated
