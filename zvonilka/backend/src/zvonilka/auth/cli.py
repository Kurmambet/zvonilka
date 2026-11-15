import argparse
import asyncio
import getpass
import sys

from zvonilka.observability.logging import configure_logging


async def execute(args, password):
    # from zvonilka.backend.src.zvonilka.config import Settings
    # from zvonilka.backend.src.zvonilka.db import create_database
    from zvonilka.config import Settings
    from zvonilka.db import create_database

    from .jwt_tokens import AccessTokens
    from .passwords import Passwords
    from .service import AuthService

    # Required values are loaded and validated from environment/.env.
    settings = Settings()  # pyright: ignore[reportCallIssue]
    engine, sessions = create_database(settings)
    try:
        passwords = Passwords()
        service = AuthService(
            sessions,
            passwords,
            AccessTokens(
                settings.jwt_secret.get_secret_value(),
                settings.jwt_issuer,
                settings.jwt_audience,
                settings.access_ttl_seconds,
            ),
        )
        if args.command == "reset-password":
            await service.reset_password(args.username, password)
            print("PASSWORD_RESET: all sessions revoked")
        else:
            count = await service.cleanup()
            print(f"AUTH_CLEANUP: deleted_sessions={count}")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset-password")
    reset.add_argument("username")
    sub.add_parser("cleanup")
    args = parser.parse_args()
    configure_logging()
    password = None
    try:
        if args.command == "reset-password":
            password = getpass.getpass("New password: ")
            if password != getpass.getpass("Repeat password: "):
                print("PASSWORD_MISMATCH", file=sys.stderr)
                return 1
        asyncio.run(execute(args, password))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 -- Process boundary; sanitized error and nonzero exit.
        print(
            f"AUTH_CLI_ERROR {type(exc).__name__}: check user/configuration/database",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
