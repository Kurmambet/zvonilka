import io
import json
import logging
import sys
import unittest
from uuid import uuid4

from zvonilka.observability.logging import (
    RepeatFilter, SafeJsonFormatter, configure_logging, log_event,
)


def record(level=logging.ERROR, **extras):
    r = logging.LogRecord("secret-logger-name", level, "/secret/path.py", 1,
                          "password=%s", ("HIDDEN_PASSWORD",), None)
    for k, v in extras.items():
        setattr(r, k, v)
    return r


class SafeLoggingTests(unittest.TestCase):
    def setUp(self):
        self.formatter = SafeJsonFormatter()

    def test_freeform_and_extras_never_emitted(self):
        r = record(safe_fields={"password": "HIDDEN_PASSWORD", "room_id": "private-room",
                                "code": "redis_unavailable", "url": "secret-url"})
        value = self.formatter.format(r)
        self.assertNotIn("HIDDEN_PASSWORD", value)
        self.assertNotIn("private-room", value)
        self.assertNotIn("secret-url", value)
        self.assertNotIn("secret-logger-name", value)
        self.assertEqual(json.loads(value)["code"], "redis_unavailable")

    def test_exception_has_reference_but_no_text_or_traceback(self):
        try:
            raise RuntimeError("refresh_token=TOP_SECRET")
        except RuntimeError:
            r = record()
            r.exc_info = sys.exc_info()
        r.exc_text = "cached secret traceback"
        r.stack_info = "secret stack"
        text = self.formatter.format(r)
        value = json.loads(text)
        self.assertEqual(value["exception_type"], "RuntimeError")
        self.assertEqual(len(value["error_ref"]), 12)
        for forbidden in ("TOP_SECRET", "cached secret", "secret stack", "Traceback"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(len(text.splitlines()), 1)

    def test_never_invokes_user_string_formatting(self):
        class Hostile:
            def __str__(self):
                raise AssertionError("should not execute")
        r = record()
        r.msg = Hostile()
        r.args = (Hostile(),)
        self.assertEqual(json.loads(self.formatter.format(r))["event"], "unexpected_error")

    def test_invalid_enum_fields_omitted(self):
        text = self.formatter.format(record(safe_event="secret-event", safe_fields={
            "code": "my-secret", "operation": "secret-op", "dependency": "secret-host",
        }))
        self.assertNotIn("my-secret", text)
        self.assertNotIn("secret-op", text)
        self.assertNotIn("secret-host", text)
        self.assertNotIn("secret-event", text)

    def test_request_id_must_be_uuid(self):
        request_id = str(uuid4())
        valid = json.loads(self.formatter.format(record(safe_fields={"request_id": request_id})))
        self.assertEqual(valid["request_id"], request_id)
        invalid = json.loads(self.formatter.format(record(safe_fields={"request_id": "SECRET"})))
        self.assertNotIn("request_id", invalid)

    def test_invalid_durations_omitted(self):
        for value in (float("nan"), float("inf"), -1, True, "secret", 86400001, 10**500):
            with self.subTest(value=value):
                parsed = json.loads(self.formatter.format(record(safe_fields={"duration_ms": value})))
                self.assertNotIn("duration_ms", parsed)

    def test_valid_duration(self):
        value = json.loads(self.formatter.format(record(safe_fields={"duration_ms": 12.34567})))
        self.assertEqual(value["duration_ms"], 12.346)

    def test_exception_class_name_is_not_arbitrary_text(self):
        SecretError = type("MySecretToken", (Exception,), {})
        try:
            raise SecretError()
        except Exception:
            r = record()
            r.exc_info = sys.exc_info()
        text = self.formatter.format(r)
        self.assertNotIn("MySecretToken", text)
        self.assertEqual(json.loads(text)["exception_type"], "Exception")

    def test_duplicates_suppressed_then_count_reported(self):
        now = [0]
        filter_ = RepeatFilter(interval=30, clock=lambda: now[0])
        self.assertTrue(filter_.filter(record()))
        for _ in range(10):
            self.assertFalse(filter_.filter(record()))
        now[0] = 30
        r = record()
        self.assertTrue(filter_.filter(r))
        self.assertEqual(r.suppressed_count, 10)
        self.assertEqual(json.loads(self.formatter.format(r))["suppressed_since_last"], 10)

    def test_suppression_memory_is_bounded(self):
        f = RepeatFilter(capacity=2)
        for op in ("join", "leave", "close"):
            self.assertTrue(f.filter(record(safe_fields={"operation": op})))
        self.assertEqual(len(f.entries), 2)

    def test_suppression_does_not_combine_different_codes(self):
        f = RepeatFilter()
        for code in ("room_limit", "invite_expired"):
            self.assertTrue(f.filter(record(safe_fields={"code": code})))

    def test_reconfigure_does_not_duplicate_root_output(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        root.handlers = []
        stream = io.StringIO()
        try:
            configure_logging(stream=stream)
            configure_logging(stream=stream)
            log_event(logging.getLogger("zvonilka"), "service_started")
            self.assertEqual(len(stream.getvalue().splitlines()), 1)
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers, root.level = handlers, level

    def test_unknown_event_rejected(self):
        with self.assertRaises(ValueError):
            log_event(logging.getLogger("zvonilka"), "free-form secret")

    def test_invalid_filter_config_rejected(self):
        for args in ({"interval": 0}, {"interval": float("nan")}, {"capacity": 0}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                RepeatFilter(**args)
