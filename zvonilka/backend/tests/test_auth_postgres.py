import asyncio
import unittest
from datetime import timedelta

# from zvonilka.backend.tests.auth_test_support import PG_URL, AuthPgFixture
from auth_test_support import PG_URL, AuthPgFixture


@unittest.skipUnless(PG_URL, "real PostgreSQL URL is required")
class AuthPostgresTests(AuthPgFixture):
    async def test_register_profile_has_no_secrets(self):
        result = await self.register("ALICE")
        self.assertEqual(result["username"], "alice")
        self.assertEqual(set(result), {"id", "username", "display_name", "created_at"})

    async def test_concurrent_registration_db_unique_constraint(self):
        from zvonilka.auth.errors import AuthError

        results = await asyncio.gather(
            *(self.register("alice") for _ in range(6)), return_exceptions=True
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(
            sum(isinstance(r, AuthError) and r.code == "username_taken" for r in results), 5
        )

    async def test_unknown_and_wrong_password(self):
        await self.assert_auth_error("invalid_credentials", self.login())
        await self.register()
        await self.assert_auth_error(
            "invalid_credentials", self.service.login("alice", "wrong-password")
        )

    async def test_login_and_access(self):
        user = await self.register()
        pair = await self.login()
        principal = await self.service.authenticate(pair["access_token"])
        self.assertEqual(principal.user_id, user["id"])
        self.assertEqual(principal.session_id, pair["session_id"])

    async def test_rotation_preserves_absolute_expiry(self):
        await self.register()
        pair = await self.login()
        rotated = await self.service.refresh(pair["refresh_token"])
        self.assertNotEqual(pair["refresh_token"], rotated["refresh_token"])
        self.assertEqual(pair["session_id"], rotated["session_id"])
        self.assertEqual(pair["refresh_expires_at"], rotated["refresh_expires_at"])
        await self.service.authenticate(rotated["access_token"])

    async def test_reuse_revocation_is_committed_not_rolled_back(self):
        from zvonilka.auth.models import RefreshSession

        await self.register()
        pair = await self.login()
        rotated = await self.service.refresh(pair["refresh_token"])
        await self.assert_auth_error("refresh_reused", self.service.refresh(pair["refresh_token"]))
        async with self.sessions() as db:
            session = await db.get(RefreshSession, pair["session_id"])
            self.assertIsNotNone(session.revoked_at)
            self.assertEqual(session.revoke_reason, "refresh_reuse")
        await self.assert_auth_error(
            "invalid_access", self.service.authenticate(rotated["access_token"])
        )
        await self.assert_auth_error(
            "invalid_refresh", self.service.refresh(rotated["refresh_token"])
        )

    async def test_concurrent_refresh_one_winner_then_family_revoked(self):
        from zvonilka.auth.errors import AuthError

        await self.register()
        pair = await self.login()
        results = await asyncio.gather(
            self.service.refresh(pair["refresh_token"]),
            self.service.refresh(pair["refresh_token"]),
            return_exceptions=True,
        )
        winners = [r for r in results if isinstance(r, dict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(
            sum(isinstance(r, AuthError) and r.code == "refresh_reused" for r in results), 1
        )
        await self.assert_auth_error(
            "invalid_refresh", self.service.refresh(winners[0]["refresh_token"])
        )

    async def test_only_hashes_are_stored(self):
        from sqlalchemy import select
        from zvonilka.auth.identity import refresh_digest
        from zvonilka.auth.models import RefreshToken, User

        await self.register()
        pair = await self.login()
        async with self.sessions() as db:
            user = await db.scalar(select(User))
            token = await db.scalar(select(RefreshToken))
            self.assertTrue(user.password_hash.startswith("$argon2id$"))
            self.assertNotIn(self.password, user.password_hash)
            self.assertEqual(token.digest, refresh_digest(pair["refresh_token"]))
            self.assertNotEqual(token.digest, pair["refresh_token"])

    async def test_logout_revokes_access_and_refresh(self):
        await self.register()
        pair = await self.login()
        principal = await self.service.authenticate(pair["access_token"])
        await self.service.revoke(principal, principal.session_id)
        await self.assert_auth_error(
            "invalid_access", self.service.authenticate(pair["access_token"])
        )
        await self.assert_auth_error("invalid_refresh", self.service.refresh(pair["refresh_token"]))

    async def test_other_account_cannot_revoke_session(self):
        await self.register()
        alice = await self.login()
        await self.register("bobby")
        bob = await self.login("bobby")
        principal = await self.service.authenticate(bob["access_token"])
        await self.assert_auth_error(
            "session_not_found", self.service.revoke(principal, alice["session_id"])
        )
        await self.service.authenticate(alice["access_token"])

    async def test_password_change_revokes_all_sessions(self):
        await self.register()
        first, second = await self.login(), await self.login()
        principal = await self.service.authenticate(first["access_token"])
        await self.service.change_password(principal, self.password, "my-new-password-456")
        for pair in (first, second):
            await self.assert_auth_error(
                "invalid_access", self.service.authenticate(pair["access_token"])
            )
            await self.assert_auth_error(
                "invalid_refresh", self.service.refresh(pair["refresh_token"])
            )
        await self.assert_auth_error("invalid_credentials", self.login())
        await self.service.login("alice", "my-new-password-456")

    async def test_wrong_current_password_does_not_revoke(self):
        await self.register()
        pair = await self.login()
        principal = await self.service.authenticate(pair["access_token"])
        await self.assert_auth_error(
            "invalid_credentials",
            self.service.change_password(principal, "wrong-password", "my-new-password-456"),
        )
        await self.service.authenticate(pair["access_token"])
        await self.service.refresh(pair["refresh_token"])

    async def test_admin_reset_revokes_existing_session(self):
        await self.register()
        pair = await self.login()
        await self.service.reset_password("alice", "admin-new-password-123")
        await self.assert_auth_error("invalid_refresh", self.service.refresh(pair["refresh_token"]))
        await self.assert_auth_error("invalid_credentials", self.login())
        await self.service.login("alice", "admin-new-password-123")

    async def test_expired_session_rejected(self):
        from sqlalchemy import update
        from zvonilka.auth.models import RefreshSession
        from zvonilka.auth.service import utcnow

        await self.register()
        pair = await self.login()
        async with self.sessions() as db, db.begin():
            await db.execute(
                update(RefreshSession).values(expires_at=utcnow() - timedelta(seconds=1))
            )
        await self.assert_auth_error(
            "invalid_access", self.service.authenticate(pair["access_token"])
        )
        await self.assert_auth_error("invalid_refresh", self.service.refresh(pair["refresh_token"]))

    async def test_session_limit_revokes_oldest(self):
        self.service.max_sessions = 2
        await self.register()
        first, second, third = await self.login(), await self.login(), await self.login()
        await self.assert_auth_error(
            "invalid_access", self.service.authenticate(first["access_token"])
        )
        await self.service.authenticate(second["access_token"])
        principal = await self.service.authenticate(third["access_token"])
        sessions = await self.service.list_sessions(principal)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sum(s["current"] for s in sessions), 1)

    async def test_disabled_user_cannot_use_session_or_login(self):
        from sqlalchemy import update
        from zvonilka.auth.models import User

        await self.register()
        pair = await self.login()
        async with self.sessions() as db, db.begin():
            await db.execute(update(User).values(enabled=False))
        await self.assert_auth_error(
            "invalid_access", self.service.authenticate(pair["access_token"])
        )
        await self.assert_auth_error("invalid_refresh", self.service.refresh(pair["refresh_token"]))
        await self.assert_auth_error("invalid_credentials", self.login())

    async def test_rotation_history_is_bounded_and_failure_commits_revocation(self):
        self.service.max_refresh_tokens = 2
        await self.register()
        pair = await self.login()
        second = await self.service.refresh(pair["refresh_token"])
        await self.assert_auth_error("refresh_limit", self.service.refresh(second["refresh_token"]))
        await self.assert_auth_error(
            "invalid_access", self.service.authenticate(second["access_token"])
        )
