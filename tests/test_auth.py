from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import AuthError, RateLimitedError
from app.services.auth import AuthService, LoginThrottle, hash_token
from tests.fakes import InMemoryUserRepo

PASSWORD = "una-clave-larga"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.mono = 0.0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        delta = timedelta(**kw)
        self.now += delta
        self.mono += delta.total_seconds()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def auth(clock):
    throttle = LoginThrottle(max_attempts=3, window_seconds=900, clock=lambda: clock.mono)
    return AuthService(InMemoryUserRepo(), throttle, session_ttl_hours=24, invite_code="pase", max_users=3, clock=clock)


def test_register_normalizes_username_and_opens_session(auth):
    user, token = auth.register("  Ana.Lopez ", PASSWORD, "pase")
    assert user.username == "ana.lopez" and user.role == "user"
    assert auth.authenticate(token)[0].id == user.id
    assert user.password_hash != PASSWORD and user.password_hash.startswith("$argon2")
    assert hash_token(token) in auth.users.sessions and token not in auth.users.sessions


@pytest.mark.parametrize("username", ["ab", "con espacio", "ñandú", "x" * 33, "a/b"])
def test_invalid_usernames_are_rejected(auth, username):
    with pytest.raises(AuthError):
        auth.register(username, PASSWORD, "pase")


def test_short_password_and_bad_invite_are_rejected(auth):
    with pytest.raises(AuthError, match="contraseña"):
        auth.register("ana", "corta", "pase")
    with pytest.raises(AuthError, match="invitación"):
        auth.register("ana", PASSWORD, "otro")


def test_duplicate_username_and_user_cap(auth):
    auth.register("ana", PASSWORD, "pase")
    with pytest.raises(AuthError, match="en uso"):
        auth.register("ANA", PASSWORD, "pase")
    auth.users.create("beto", None)
    auth.users.create("caro", None)
    with pytest.raises(AuthError, match="lugares"):
        auth.register("dani", PASSWORD, "pase")


def test_login_errors_do_not_reveal_whether_user_exists(auth):
    auth.register("ana", PASSWORD, "pase")
    with pytest.raises(AuthError) as wrong_password:
        auth.login("ana", "incorrecta-123")
    with pytest.raises(AuthError) as unknown_user:
        auth.login("nadie", "incorrecta-123")
    assert str(wrong_password.value) == str(unknown_user.value)


def test_login_is_throttled_and_window_expires(auth, clock):
    auth.register("ana", PASSWORD, "pase")
    for _ in range(3):
        with pytest.raises(AuthError):
            auth.login("ana", "incorrecta-123")
    with pytest.raises(RateLimitedError):
        auth.login("ana", PASSWORD)  # bloqueada aunque ahora la contraseña sea correcta
    clock.advance(minutes=16)
    assert auth.login("ana", PASSWORD)[0].username == "ana"


def test_session_expires_and_renews_while_used(auth, clock):
    _, token = auth.register("ana", PASSWORD, "pase")
    clock.advance(hours=13)  # pasó más de la mitad del TTL (24 h) → se renueva
    assert auth.authenticate(token)[1] is True
    clock.advance(hours=23)  # sigue vigente gracias a la renovación
    assert auth.authenticate(token) is not None
    clock.advance(hours=25)
    assert auth.authenticate(token) is None
    assert hash_token(token) not in auth.users.sessions  # la sesión vencida se borra


def test_logout_and_password_change_revoke_sessions(auth):
    user, phone = auth.register("ana", PASSWORD, "pase")
    _, laptop = auth.login("ana", PASSWORD)
    with pytest.raises(AuthError, match="actual"):
        auth.change_password(user, "no-es-esta", "otra-clave-larga", laptop)
    auth.change_password(user, PASSWORD, "otra-clave-larga", laptop)
    assert auth.authenticate(laptop) is not None   # la sesión desde donde se cambió sigue
    assert auth.authenticate(phone) is None        # las demás se cierran
    auth.logout(laptop)
    assert auth.authenticate(laptop) is None
    assert auth.login("ana", "otra-clave-larga")


def test_local_user_without_password_cannot_log_in(auth):
    local = auth.ensure_user("local")
    assert auth.ensure_user("LOCAL").id == local.id
    with pytest.raises(AuthError):
        auth.login("local", "")


def test_admin_cli_helpers(auth):
    auth.create_user("root", PASSWORD, role="admin")
    assert auth.login("root", PASSWORD)[0].is_admin
    auth.reset_password("root", "nueva-clave-larga")
    assert auth.login("root", "nueva-clave-larga")
    assert auth.set_role("root", "user").role == "user"
