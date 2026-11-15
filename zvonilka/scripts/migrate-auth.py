"""Run using backend environment: cd backend; uv run python ../scripts/migrate-auth.py"""

import os
import sys
from pathlib import Path

backend = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(backend / "src"))
os.chdir(backend)


def main():
    try:
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config(str(backend / "alembic.ini")), "head")

    except ModuleNotFoundError as exc:
        print(
            f"MIGRATION_ERROR missing_module={exc.name}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    except Exception as exc:  # noqa: BLE001 -- Migration CLI boundary; safe diagnostics and nonzero exit.
        print(
            f"MIGRATION_ERROR {type(exc).__name__}: check database URL, schema permissions and revision",
            file=sys.stderr,
        )
        return 1
    print("MIGRATION_OK head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
