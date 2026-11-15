import importlib.util
import unittest


@unittest.skipUnless(
    importlib.util.find_spec("pydantic_settings"), "pydantic-settings dependency is required"
)
class ConfigTests(unittest.TestCase):
    def create(self, **changes):
        from pydantic import SecretStr
        from zvonilka.config import Settings

        return Settings(
            _env_file=None,
            **{
                "database_url": SecretStr("postgresql+asyncpg://u:p@localhost/test"),
                "jwt_secret": SecretStr("a-random-enough-test-secret-" + "x" * 32),
                **changes,
            },
        )

    def test_invalid_or_placeholder_secret(self):
        from pydantic import SecretStr, ValidationError

        for secret in ("short", "REPLACE_WITH_RANDOM_SECRET_AT_LEAST_32_BYTES"):
            with self.subTest(secret=secret), self.assertRaises(ValidationError):
                self.create(jwt_secret=SecretStr(secret))

    def test_schema_cannot_be_sql(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            self.create(database_schema="public; DROP SCHEMA public")

    def test_access_ttl_and_refresh_ttl(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            self.create(access_ttl_seconds=3600, refresh_ttl_seconds=3600)
