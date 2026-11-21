import argparse
import sys

from zvonilka.observability.logging import configure_logging


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    configure_logging()
    try:
        import uvicorn

        from .api import create_app

        app = create_app()
        uvicorn.run(
            app,
            host=args.host,
            port=8000,
            workers=1,
            ws="websockets",
            ws_max_size=16384,
            ws_max_queue=4,
            ws_per_message_deflate=False,
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=5,
        )
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 -- Process boundary; sanitized error and nonzero exit.
        # Settings validation/DSN errors may contain credentials; never print str(exc).
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            from .config import Settings

            names = sorted(
                {
                    str(e["loc"][0])
                    for e in exc.errors(
                        include_input=False, include_context=False, include_url=False
                    )
                    if e["loc"] and e["loc"][0] in Settings.model_fields
                }
            )
            print("CONFIG_ERROR fields=" + ",".join(names[:10]), file=sys.stderr)
        else:
            print(
                f"STARTUP_ERROR {type(exc).__name__}: check configuration and dependencies",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
