"""Cuentas, contraseñas y sesiones.

- Contraseñas con Argon2 (pwdlib). Se re-hashean solas si cambian los parámetros recomendados.
- Sesiones opacas: el token aleatorio va en una cookie HttpOnly; en la BD solo se guarda su SHA-256, así que una
  copia de la BD no sirve para suplantar a nadie. Se renuevan solas mientras se usen (expiración deslizante).
- Inicio de sesión con límite de intentos por usuario y por IP, y mismo tiempo de respuesta exista o no el usuario.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash

from app.core.exceptions import AuthError, RateLimitedError
from app.schemas import UserAccount

USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
MIN_PASSWORD, MAX_PASSWORD = 10, 128
INVALID_LOGIN = "Usuario o contraseña incorrectos."


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


class LoginThrottle:
    """Ventana deslizante en memoria. Suficiente con una sola instancia (Render free corre un proceso)."""

    def __init__(self, max_attempts: int, window_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str) -> deque[float]:
        q, limit = self._failures[key], self._clock() - self.window
        while q and q[0] < limit:
            q.popleft()
        return q

    def check(self, *keys: str, max_attempts: int | None = None) -> None:
        limit = max_attempts or self.max_attempts
        with self._lock:
            if any(len(self._recent(k)) >= limit for k in keys):
                minutes = max(1, round(self.window / 60))
                raise RateLimitedError(f"Demasiados intentos. Espera {minutes} minutos e inténtalo de nuevo.")

    def fail(self, *keys: str) -> None:
        with self._lock:
            now = self._clock()
            for k in keys:
                self._recent(k).append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures.pop(k, None)


class AuthService:
    def __init__(
        self,
        users,
        throttle: LoginThrottle,
        session_ttl_hours: int = 24 * 7,
        invite_code: str | None = None,
        max_users: int = 0,
        hasher: PasswordHash | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.users = users
        self.throttle = throttle
        self.ttl = timedelta(hours=session_ttl_hours)
        self.invite_code = invite_code or None
        self.max_users = max_users
        self._hasher = hasher or PasswordHash.recommended()
        self._clock = clock
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(16))

    @property
    def invite_required(self) -> bool:
        return self.invite_code is not None

    # ------------------------------------------------------------- validaciones
    @staticmethod
    def validate_username(username: str) -> str:
        username = normalize_username(username)
        if not USERNAME_RE.fullmatch(username):
            raise AuthError("El usuario debe tener de 3 a 32 caracteres: letras, números, punto, guion o guion bajo.")
        return username

    @staticmethod
    def validate_password(password: str) -> str:
        if not MIN_PASSWORD <= len(password or "") <= MAX_PASSWORD:
            raise AuthError(f"La contraseña debe tener entre {MIN_PASSWORD} y {MAX_PASSWORD} caracteres.")
        return password

    # ------------------------------------------------------------- flujo web
    def register(
        self, username: str, password: str, invite_code: str | None = None,
        ip: str = "-", user_agent: str | None = None,
    ) -> tuple[UserAccount, str]:  # fmt: skip
        self.throttle.check(f"register:{ip}")
        if self.invite_code and not secrets.compare_digest((invite_code or "").strip(), self.invite_code):
            self.throttle.fail(f"register:{ip}")
            raise AuthError("El código de invitación no es válido.")
        username = self.validate_username(username)
        self.validate_password(password)
        if self.max_users and self.users.count_users() >= self.max_users:
            raise AuthError("Ya no hay lugares disponibles para nuevas cuentas.")
        if self.users.get_by_username(username):
            raise AuthError("Ese nombre de usuario ya está en uso.")
        user = self.users.create(username, self._hasher.hash(password))
        self.throttle.fail(f"register:{ip}")  # cuenta también las altas exitosas: frena la creación masiva
        return user, self._new_session(user, user_agent)

    def login(
        self, username: str, password: str, ip: str = "-", user_agent: str | None = None
    ) -> tuple[UserAccount, str]:
        username = normalize_username(username)
        keys = (f"user:{username}", f"ip:{ip}")
        self.throttle.check(keys[0])
        self.throttle.check(keys[1], max_attempts=self.throttle.max_attempts * 4)  # IPs compartidas (NAT)

        user = self.users.get_by_username(username)
        stored = user.password_hash if user and user.is_active else None
        valid, new_hash = self._hasher.verify_and_update(password or "", stored or self._dummy_hash)
        if not (valid and stored):
            self.throttle.fail(*keys)
            raise AuthError(INVALID_LOGIN)

        self.throttle.reset(keys[0])
        if new_hash:
            self.users.set_password(user.id, new_hash)
        self.users.touch_login(user.id)
        return user, self._new_session(user, user_agent)

    def authenticate(self, token: str | None) -> tuple[UserAccount, bool] | None:
        """Devuelve (usuario, renovada) o None si el token no corresponde a una sesión vigente."""
        if not token:
            return None
        token_hash = hash_token(token)
        found = self.users.get_session(token_hash)
        if not found:
            return None
        user, expires_at = found
        now = self._clock()
        if expires_at <= now or not user.is_active:
            self.users.delete_session(token_hash)
            return None
        renewed = expires_at - now < self.ttl / 2
        if renewed:
            self.users.extend_session(token_hash, now + self.ttl)
        return user, renewed

    def logout(self, token: str | None) -> None:
        if token:
            self.users.delete_session(hash_token(token))

    def change_password(self, user: UserAccount, current: str, new: str, current_token: str | None) -> None:
        fresh = self.users.get_by_id(user.id)
        if not fresh or not fresh.password_hash or not self._hasher.verify(current or "", fresh.password_hash):
            raise AuthError("La contraseña actual no es correcta.")
        self.validate_password(new)
        self.users.set_password(user.id, self._hasher.hash(new))
        # Cierra las demás sesiones (otro navegador, un equipo prestado...), conserva la actual.
        self.users.delete_user_sessions(user.id, except_token_hash=hash_token(current_token) if current_token else None)

    # ------------------------------------------------------------- administración (CLI)
    def create_user(self, username: str, password: str | None, role: str = "user") -> UserAccount:
        username = self.validate_username(username)
        if self.users.get_by_username(username):
            raise AuthError("Ese nombre de usuario ya está en uso.")
        password_hash = self._hasher.hash(self.validate_password(password)) if password is not None else None
        return self.users.create(username, password_hash, role)

    def reset_password(self, username: str, password: str) -> UserAccount:
        user = self.users.get_by_username(normalize_username(username))
        if not user:
            raise AuthError(f"No existe el usuario {username!r}.")
        self.users.set_password(user.id, self._hasher.hash(self.validate_password(password)))
        self.users.delete_user_sessions(user.id)
        return user

    def set_role(self, username: str, role: str) -> UserAccount:
        if role not in ("user", "admin"):
            raise AuthError("El rol debe ser 'user' o 'admin'.")
        user = self.users.get_by_username(normalize_username(username))
        if not user:
            raise AuthError(f"No existe el usuario {username!r}.")
        self.users.set_role(user.id, role)
        return self.users.get_by_id(user.id)

    def ensure_user(self, username: str) -> UserAccount:
        """Usuario local de la GUI/CLI: lo crea sin contraseña (no puede entrar por la web) si no existe."""
        username = self.validate_username(username)
        return self.users.get_by_username(username) or self.users.create(username, None)

    # ------------------------------------------------------------- interno
    def _new_session(self, user: UserAccount, user_agent: str | None) -> str:
        token = secrets.token_urlsafe(32)
        self.users.create_session(user.id, hash_token(token), self._clock() + self.ttl, user_agent)
        return token
