import unittest

from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsConfigDict
from zvonilka.config import Settings


class WithoutDotenv(Settings):
    # Inherited settings configuration is preserved; only dotenv loading is off.
    # OS environment is still supported, as with the previous _env_file=None.
    model_config = SettingsConfigDict(env_file=None)


class RoomConfigTests(unittest.TestCase):
    def settings(self, **changes):
        return WithoutDotenv(
            database_url=SecretStr("postgresql+asyncpg://test:test@localhost/test"),
            jwt_secret=SecretStr("unit-only-secret-" + "x" * 48),
            **changes,
        )

    def test_settings_map_to_repository_limits(self):
        cfg = self.settings(
            max_active_rooms=4,
            max_active_rooms_per_user=2,
            room_pending_ttl_seconds=300,
            room_empty_grace_seconds=12,
            max_pending_rooms_per_user=5,
        )
        limits = cfg.room_limits()
        self.assertEqual(limits.rooms, 4)
        self.assertEqual(limits.owner_rooms, 2)
        self.assertEqual(limits.pending_ttl, 300)
        self.assertEqual(limits.grace, 12)
        self.assertEqual(limits.pending_per_owner, 5)

    def test_unsafe_configuration_rejected(self):
        for changes in (
            {"max_participants_per_room": 4},
            {"room_registry_max_records": 129},
            {"room_invites_per_room": 33},
            {"room_registry_key": "bad key"},
            {"room_write_rate_limit": 0},
            {"admission_mode": "guarded"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.settings(**changes)

    def test_default_aggregate_key(self):
        self.assertEqual(self.settings().room_registry_key, "zvonilka:{rooms}:v1")
