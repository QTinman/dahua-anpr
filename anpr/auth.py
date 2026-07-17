"""Simple username/password authentication with server-side sessions.

First run shows a setup screen to create an admin account (or skip, to keep
running without a login on a trusted LAN). Once a user exists, the API and
WebSocket require a valid session cookie.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib, no dependencies).
Sessions are random tokens stored in the database so they survive restarts and
can be revoked on logout.
"""

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime
from typing import Optional

from .database import Database

SESSION_COOKIE = "anpr_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
PBKDF2_ITERATIONS = 200_000
AUTH_DISABLED_KEY = "auth_disabled"


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


class AuthManager:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- state

    def env_disabled(self) -> bool:
        return os.environ.get("ANPR_NO_AUTH") == "1"

    def is_disabled(self) -> bool:
        """Auth is off if explicitly disabled (skipped at setup or via env)."""
        return self.env_disabled() or bool(self.db.get_setting(AUTH_DISABLED_KEY))

    def has_users(self) -> bool:
        return self.db.count_users() > 0

    def needs_setup(self) -> bool:
        """First run: no users yet and not explicitly disabled."""
        return not self.env_disabled() and not self.has_users() \
            and not self.db.get_setting(AUTH_DISABLED_KEY)

    def auth_required(self) -> bool:
        return not self.is_disabled() and self.has_users()

    # ------------------------------------------------------------- users

    def create_user(self, username: str, password: str) -> None:
        username = username.strip()
        if not username or not password:
            raise ValueError("Username and password are required")
        if len(password) < 6:
            raise ValueError("Password must be at least 6 characters")
        salt = secrets.token_bytes(16)
        self.db.add_user(username, salt.hex(), _hash(password, salt),
                         datetime.now().isoformat(timespec="seconds"))
        # Creating a user implicitly enables auth.
        self.db.set_setting(AUTH_DISABLED_KEY, False)

    def setup(self, username: str, password: str) -> None:
        if self.has_users():
            raise ValueError("Setup already completed")
        self.create_user(username, password)

    def disable(self) -> None:
        """Skip login at setup - run without authentication."""
        self.db.set_setting(AUTH_DISABLED_KEY, True)

    def verify_password(self, username: str, password: str) -> bool:
        user = self.db.get_user(username.strip())
        if not user:
            return False
        expected = user["password_hash"]
        actual = _hash(password, bytes.fromhex(user["salt"]))
        return hmac.compare_digest(expected, actual)

    def change_password(self, username: str, old: str, new: str) -> None:
        if not self.verify_password(username, old):
            raise ValueError("Current password is incorrect")
        if len(new) < 6:
            raise ValueError("New password must be at least 6 characters")
        salt = secrets.token_bytes(16)
        self.db.update_user_password(username, salt.hex(), _hash(new, salt))

    # ---------------------------------------------------------- sessions

    def login(self, username: str, password: str) -> Optional[str]:
        if not self.verify_password(username, password):
            return None
        token = secrets.token_urlsafe(32)
        self.db.add_session(token, username.strip(), time.time() + SESSION_TTL_SECONDS)
        return token

    def validate(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        row = self.db.get_session(token)
        if not row:
            return None
        if row["expires_at"] < time.time():
            self.db.delete_session(token)
            return None
        return row["username"]

    def logout(self, token: Optional[str]) -> None:
        if token:
            self.db.delete_session(token)

    def purge_expired(self) -> None:
        self.db.delete_expired_sessions(time.time())
