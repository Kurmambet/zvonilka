import os
import unittest

# from zvonilka.backend.tests.auth_test_support import PG_URL, AuthPgFixture
from auth_test_support import PG_URL, AuthPgFixture

REDIS_URL = os.environ.get("ZVONILKA_TEST_REDIS_URL")


@unittest.skipUnless(PG_URL and REDIS_URL, "real PostgreSQL and Redis URLs are required")
class AuthApiTests(AuthPgFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        import httpx
        from pydantic import SecretStr
        from zvonilka.api import create_app

        assert REDIS_URL is not None
        self.settings.redis_url = SecretStr(REDIS_URL)
        self.asgi = create_app(self.settings)
        self.context = self.asgi.app.router.lifespan_context(self.asgi.app)
        await self.context.__aenter__()
        self.addAsyncCleanup(self.context.__aexit__, None, None, None)
        self.addAsyncCleanup(self.cleanup_rate_keys)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.asgi), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def cleanup_rate_keys(self):
        redis = self.asgi.app.state.redis
        keys = [key async for key in redis.scan_iter(match=self.settings.rate_limit_prefix + "*")]
        if keys:
            await redis.delete(*keys)

    async def api_register(self):
        return await self.client.post(
            "/api/v1/auth/register",
            json={"username": "alice", "display_name": "Алиса", "password": self.password},
        )

    async def api_login(self):
        return await self.client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": self.password}
        )

    async def test_register_login_profile_and_logout(self):
        registered = await self.api_register()
        self.assertEqual(registered.status_code, 201, registered.text)
        self.assertEqual(set(registered.json()), {"id", "username", "display_name", "created_at"})
        pair = await self.api_login()
        self.assertEqual(pair.status_code, 200, pair.text)
        headers = {"Authorization": "Bearer " + pair.json()["access_token"]}
        profile = await self.client.get("/api/v1/users/me", headers=headers)
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["username"], "alice")
        self.assertEqual(
            (await self.client.post("/api/v1/auth/logout", headers=headers)).status_code, 204
        )
        self.assertEqual(
            (await self.client.get("/api/v1/users/me", headers=headers)).status_code, 401
        )

    async def test_validation_response_never_echoes_password(self):
        response = await self.client.post(
            "/api/v1/auth/register",
            json={"username": "!", "display_name": "A", "password": "TOP_SECRET"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("TOP_SECRET", response.text)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_exact_user_lookup(self):
        path = "/api/v1/users/lookup"
        self.assertEqual((await self.api_register()).status_code, 201)
        pair = await self.api_login()
        self.assertEqual(pair.status_code, 200)
        headers = {"Authorization": "Bearer " + pair.json()["access_token"]}
        unauthenticated = await self.client.get(path, params={"username": "alice"})
        self.assertEqual(unauthenticated.status_code, 401)
        response = await self.client.get(path, params={"username": " ALICE "}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"username": "alice", "display_name": "Алиса"})
        prefix = await self.client.get(path, params={"username": "ali"}, headers=headers)
        self.assertEqual(prefix.status_code, 404)
        self.assertEqual(prefix.json()["error"]["code"], "user_not_found")
        invalid = await self.client.get(path, params={"username": "!"}, headers=headers)
        self.assertEqual(invalid.status_code, 422)
        for _ in range(17):
            response = await self.client.get(path, params={"username": "ali"}, headers=headers)
            self.assertEqual(response.status_code, 404)
        limited = await self.client.get(path, params={"username": "alice"}, headers=headers)
        self.assertEqual(limited.status_code, 429)

    async def test_body_limit(self):
        response = await self.client.post("/api/v1/auth/login", content=b"x" * 17000)
        self.assertEqual(response.status_code, 413)

    async def test_rate_limit_is_enforced_by_real_redis(self):
        from zvonilka.auth.errors import AuthError

        limiter = self.asgi.app.state.limiter
        for _ in range(3):
            await limiter.check("integration", "same-identity", 3, 60)
        with self.assertRaises(AuthError) as ctx:
            await limiter.check("integration", "same-identity", 3, 60)
        self.assertEqual(ctx.exception.status, 429)

    async def test_unauthenticated_and_missing_route_are_safe_json(self):
        for path, status in (("/api/v1/users/me", 401), ("/not-found", 404)):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, status)
            self.assertIn("request_id", response.json()["error"])
            self.assertEqual(response.headers["cache-control"], "no-store")
