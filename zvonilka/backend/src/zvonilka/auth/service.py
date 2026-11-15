from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import AuthError
from .identity import (
    new_refresh_token,
    normalize_username,
    refresh_digest,
    validate_display_name,
    validate_password,
)
from .jwt_tokens import AccessTokens
from .models import RefreshSession, RefreshToken, User
from .passwords import Passwords


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    session_id: UUID
    username: str
    display_name: str
    created_at: datetime

    def profile(self) -> dict:
        return {
            "id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "created_at": self.created_at,
        }


class AuthService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        passwords: Passwords,
        access: AccessTokens,
        *,
        refresh_ttl: int = 1209600,
        max_sessions: int = 10,
        max_refresh_tokens: int = 4096,
    ):
        self.sessions, self.passwords, self.access = sessions, passwords, access
        self.refresh_ttl, self.max_sessions = refresh_ttl, max_sessions
        self.max_refresh_tokens = max_refresh_tokens

    @staticmethod
    def _profile(user: User) -> dict:
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "created_at": user.created_at,
        }

    async def register(self, username: str, display_name: str, password: str) -> dict:
        username, display_name = normalize_username(username), validate_display_name(display_name)
        encoded = await self.passwords.hash(password)
        try:
            async with self.sessions() as db, db.begin():
                user = User(username=username, display_name=display_name, password_hash=encoded)
                db.add(user)
                await db.flush()
                profile = self._profile(user)
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise AuthError("username_taken", 409) from None
            raise
        return profile

    def _pair(self, user_id: UUID, session: RefreshSession, raw_refresh: str) -> dict:
        return {
            "access_token": self.access.issue(user_id, session.id),
            "refresh_token": raw_refresh,
            "token_type": "bearer",
            "expires_in": self.access.ttl,
            "refresh_expires_at": session.expires_at,
            "session_id": session.id,
        }

    async def login(self, username: str, password: str) -> dict:
        username = normalize_username(username)
        async with self.sessions() as db:
            user = await db.scalar(select(User).where(User.username == username))
            old_hash = user.password_hash if user else None
            user_id = user.id if user else None
        if not await self.passwords.verify(old_hash, password) or user_id is None:
            raise AuthError("invalid_credentials")
        replacement = None
        if old_hash is not None and await self.passwords.needs_rehash(old_hash):
            replacement = await self.passwords.hash(password)
        raw = new_refresh_token()
        async with self.sessions() as db, db.begin():
            user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None or not user.enabled or user.password_hash != old_hash:
                raise AuthError("invalid_credentials")
            if replacement is not None:
                user.password_hash = replacement
            now = utcnow()
            active = list(
                (
                    await db.scalars(
                        select(RefreshSession)
                        .where(
                            RefreshSession.user_id == user.id,
                            RefreshSession.revoked_at.is_(None),
                            RefreshSession.expires_at > now,
                        )
                        .order_by(RefreshSession.created_at, RefreshSession.id)
                    )
                ).all()
            )
            for old in active[: max(0, len(active) - self.max_sessions + 1)]:
                old.revoked_at, old.revoke_reason = now, "session_limit"
            session = RefreshSession(
                id=uuid4(),
                user_id=user.id,
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(seconds=self.refresh_ttl),
            )
            db.add(session)
            await db.flush()
            db.add(RefreshToken(digest=refresh_digest(raw), session_id=session.id, created_at=now))
            result = self._pair(user.id, session, raw)
        return result

    async def refresh(self, raw: str) -> dict:
        try:
            digest = refresh_digest(raw)
        except ValueError:
            raise AuthError("invalid_refresh") from None
        failure = None
        result = None
        replacement = new_refresh_token()
        async with self.sessions() as db, db.begin():
            row = (
                await db.execute(
                    select(RefreshSession.user_id, RefreshToken.session_id)
                    .join(RefreshToken, RefreshToken.session_id == RefreshSession.id)
                    .where(RefreshToken.digest == digest)
                )
            ).one_or_none()
            if row is None:
                raise AuthError("invalid_refresh")
            user_id, session_id = row
            # Global order for this subsystem: user -> session -> token.
            user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
            session = await db.scalar(
                select(RefreshSession).where(RefreshSession.id == session_id).with_for_update()
            )
            token = await db.scalar(
                select(RefreshToken).where(RefreshToken.digest == digest).with_for_update()
            )
            now = utcnow()
            if (
                user is None
                or not user.enabled
                or session is None
                or token is None
                or session.revoked_at is not None
                or session.expires_at <= now
            ):
                failure = "invalid_refresh"
            elif token.consumed_at is not None:
                session.revoked_at, session.revoke_reason = now, "refresh_reuse"
                failure = "refresh_reused"
            else:
                token_count = await db.scalar(
                    select(func.count())
                    .select_from(RefreshToken)
                    .where(RefreshToken.session_id == session.id)
                )
                if token_count is not None and token_count >= self.max_refresh_tokens:
                    session.revoked_at, session.revoke_reason = now, "rotation_limit"
                    failure = "refresh_limit"
                else:
                    token.consumed_at = now
                    session.last_seen_at = now
                    db.add(
                        RefreshToken(
                            digest=refresh_digest(replacement),
                            session_id=session.id,
                            created_at=now,
                        )
                    )
                    result = self._pair(user.id, session, replacement)
        # Crucial: raise AFTER commit, otherwise reuse revocation is rolled back.
        if failure is not None:
            raise AuthError(failure)
        if result is None:
            raise RuntimeError("refresh produced no result")
        return result

    async def authenticate(self, access_token: str) -> Principal:
        user_id, session_id = self.access.decode(access_token)
        async with self.sessions() as db:
            row = (
                await db.execute(
                    select(User, RefreshSession)
                    .join(RefreshSession, RefreshSession.user_id == User.id)
                    .where(User.id == user_id, RefreshSession.id == session_id)
                )
            ).one_or_none()
            if row is None:
                raise AuthError("invalid_access")
            user, session = row
            if not user.enabled or session.revoked_at is not None or session.expires_at <= utcnow():
                raise AuthError("invalid_access")
            return Principal(user.id, session.id, user.username, user.display_name, user.created_at)

    async def _lock_principal(self, db: AsyncSession, principal: Principal) -> User:
        user = await db.scalar(select(User).where(User.id == principal.user_id).with_for_update())
        session = await db.scalar(
            select(RefreshSession).where(
                RefreshSession.id == principal.session_id,
                RefreshSession.user_id == principal.user_id,
            )
        )
        if (
            user is None
            or not user.enabled
            or session is None
            or session.revoked_at is not None
            or session.expires_at <= utcnow()
        ):
            raise AuthError("invalid_access")
        return user

    async def list_sessions(self, principal: Principal) -> list[dict]:
        async with self.sessions() as db:
            sessions = (
                await db.scalars(
                    select(RefreshSession)
                    .where(
                        RefreshSession.user_id == principal.user_id,
                        RefreshSession.revoked_at.is_(None),
                        RefreshSession.expires_at > utcnow(),
                    )
                    .order_by(RefreshSession.created_at)
                )
            ).all()
            return [
                {
                    "id": s.id,
                    "created_at": s.created_at,
                    "last_seen_at": s.last_seen_at,
                    "expires_at": s.expires_at,
                    "current": s.id == principal.session_id,
                }
                for s in sessions
            ]

    async def revoke(self, principal: Principal, target: UUID) -> None:
        async with self.sessions() as db, db.begin():
            await self._lock_principal(db, principal)
            session = await db.scalar(
                select(RefreshSession).where(
                    RefreshSession.id == target, RefreshSession.user_id == principal.user_id
                )
            )
            if session is None:
                raise AuthError("session_not_found", 404)
            if session.revoked_at is None:
                session.revoked_at, session.revoke_reason = utcnow(), "logout"

    async def change_password(self, principal: Principal, current: str, new: str) -> None:
        validate_password(new)
        # Verification is off event loop; same per-user lock prevents races with refresh/login.
        async with self.sessions() as db, db.begin():
            user = await self._lock_principal(db, principal)
            if not await self.passwords.verify(user.password_hash, current):
                raise AuthError("invalid_credentials")
            user.password_hash = await self.passwords.hash(new)
            await db.execute(
                update(RefreshSession)
                .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
                .values(revoked_at=utcnow(), revoke_reason="password_changed")
            )

    async def reset_password(self, username: str, password: str) -> None:
        encoded = await self.passwords.hash(password)
        async with self.sessions() as db, db.begin():
            user = await db.scalar(
                select(User).where(User.username == normalize_username(username)).with_for_update()
            )
            if user is None:
                raise AuthError("user_not_found", 404)
            user.password_hash = encoded
            await db.execute(
                update(RefreshSession)
                .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
                .values(revoked_at=utcnow(), revoke_reason="admin_reset")
            )

    async def cleanup(self) -> int:
        async with self.sessions() as db, db.begin():
            victims = (
                select(RefreshSession.id)
                .where(
                    or_(
                        RefreshSession.expires_at < utcnow() - timedelta(days=7),
                        RefreshSession.revoked_at < utcnow() - timedelta(days=7),
                    )
                )
                .limit(1000)
            )
            result = await db.scalars(
                delete(RefreshSession)
                .where(RefreshSession.id.in_(victims))
                .returning(RefreshSession.id)
            )
            return len(result.all())
