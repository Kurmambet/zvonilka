"""Allowlisted single-line logging. No arbitrary message/exception text emitted."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sys
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TextIO
from uuid import UUID

EVENTS = {
    "service_started": "Service started",
    "service_stopped": "Service stopped",
    "dependency_unavailable": "Required dependency is unavailable",
    "operation_rejected": "Operation rejected",
    "unexpected_error": "Unexpected internal error",
    "background_task_failed": "Background task failed",
}
CODES = frozenset(
    {
        "room_id_exists",
        "room_not_found",
        "room_closed",
        "room_limit",
        "owner_room_limit",
        "pending_limit",
        "participant_limit",
        "not_owner",
        "not_admitted",
        "overloaded",
        "stale_generation",
        "invite_exists",
        "invite_limit",
        "invite_history_limit",
        "invite_not_found",
        "invite_consumed",
        "invite_revoked",
        "invite_expired",
        "registry_full",
        "schema_mismatch",
        "redis_unavailable",
        "postgres_unavailable",
        "timeout",
        "internal_error",
    }
)
OPERATIONS = frozenset(
    {
        "create",
        "snapshot",
        "join",
        "leave",
        "heartbeat",
        "close",
        "issue_invite",
        "revoke_invite",
        "redeem",
        "sweep",
    }
)
DEPENDENCIES = frozenset({"redis", "postgres", "turn"})
EXCEPTIONS = frozenset(
    {
        "ConnectionError",
        "TimeoutError",
        "OSError",
        "ValueError",
        "TypeError",
        "RuntimeError",
        "RoomError",
        "ResponseError",
        "AuthenticationError",
        "CancelledError",
        "AssertionError",
    }
)


def _safe_fields(values: object) -> dict:
    if type(values) is not dict:
        return {}
    output = {}
    for key, allowed in (("code", CODES), ("operation", OPERATIONS), ("dependency", DEPENDENCIES)):
        value = values.get(key)
        if type(value) is str and value in allowed:
            output[key] = value
    value = values.get("request_id")
    if type(value) is str:
        try:
            output["request_id"] = str(UUID(value))
        except ValueError:
            pass
    value = values.get("duration_ms")
    if type(value) in (int, float) and 0 <= value <= 86400000 and math.isfinite(value):
        output["duration_ms"] = round(value, 3)
    return output


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Deliberately never calls getMessage(), repr(exception), formatException,
        # or formats arbitrary extras, args, stack_info or cached exc_text.
        event = getattr(record, "safe_event", None)
        if type(event) is not str or event not in EVENTS:
            event = "unexpected_error" if record.levelno >= logging.ERROR else "external_event"
        item = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": logging.getLevelName(record.levelno)
            if record.levelno in (10, 20, 30, 40, 50)
            else "INFO",
            "event": event,
            "message": EVENTS.get(event, "External library event; free-form text omitted"),
        }
        item.update(_safe_fields(getattr(record, "safe_fields", {})))
        if record.exc_info and record.exc_info[0]:
            name = record.exc_info[0].__name__
            item["exception_type"] = name if name in EXCEPTIONS else "Exception"
            tb = record.exc_info[2]
            locations = []
            while tb is not None:
                locations.append(f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}")
                tb = tb.tb_next
            item["error_ref"] = hashlib.sha256("|".join(locations).encode()).hexdigest()[:12]
        suppressed = getattr(record, "suppressed_count", 0)
        if type(suppressed) is int and suppressed > 0:
            item["suppressed_since_last"] = suppressed
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))


class RepeatFilter(logging.Filter):
    """Bounded duplicate suppression for WARNING+; counts reported on next emission.

    This is not log rotation or a global traffic limiter. State has a hard cap;
    different error_ref values of one event/code are intentionally grouped.
    """

    def __init__(self, interval: float = 30, capacity: int = 256, clock=time.monotonic):
        super().__init__()
        if (
            not math.isfinite(interval)
            or interval <= 0
            or type(capacity) is not int
            or capacity < 1
        ):
            raise ValueError("invalid repeat filter configuration")
        self.interval, self.capacity, self.clock = interval, capacity, clock
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        fields = _safe_fields(getattr(record, "safe_fields", {}))
        event = getattr(record, "safe_event", None)
        event = event if type(event) is str and event in EVENTS else "external_event"
        key = (
            record.levelno,
            event,
            fields.get("code"),
            fields.get("operation"),
            fields.get("dependency"),
        )
        now = self.clock()
        with self.lock:
            old = self.entries.pop(key, None)
            if old is not None and now - old[0] < self.interval:
                self.entries[key] = (old[0], old[1] + 1)
                return False
            record.suppressed_count = old[1] if old else 0
            self.entries[key] = (now, 0)
            if len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
        return True


def configure_logging(
    *, stream: TextIO | None = None, level: int = logging.INFO, repeat_interval: float = 30
) -> None:
    """Call once at service entrypoint; reconfiguration replaces root handlers.

    Future uvicorn must use log_config=None and access_log=False. Named library
    handlers must be audited separately; this cannot sanitize arbitrary files,
    print(), warnings or handlers installed later.
    """
    if level not in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL):
        raise ValueError("invalid log level")
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(SafeJsonFormatter())
    handler.addFilter(RepeatFilter(repeat_interval))
    logger = logging.getLogger()
    old = logger.handlers[:]
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    for previous in old:
        previous.close()


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: object,
) -> None:
    if type(event) is not str or event not in EVENTS:
        raise ValueError("unknown log event")
    if level not in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL):
        raise ValueError("invalid log level")
    logger.log(
        level,
        "",
        extra={"safe_event": event, "safe_fields": _safe_fields(fields)},
        exc_info=exc_info,
    )
