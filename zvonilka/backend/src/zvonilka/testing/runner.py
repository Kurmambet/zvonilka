"""Compact unittest CLI; failures keep nonzero exit codes, skips stay visible."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
import unittest
from pathlib import Path

from zvonilka.observability.logging import configure_logging


def compact(text: str, limit: int = 400) -> str:
    # json escaping prevents ANSI/newlines/control characters from altering output.
    value = str(text)
    if len(value) > limit:
        value = value[:limit] + "...[truncated]"
    return json.dumps(value, ensure_ascii=False)[1:-1]


class CompactResult(unittest.TestResult):
    def __init__(self, *, stream, tracebacks=False, max_details=10):
        super().__init__()
        self.stream = stream
        self.tracebacks = tracebacks
        self.max_details = max_details
        self.detail_count = 0
        self.successes = 0
        self.bad_subtests = 0

    def _exc_info_to_string(self, err, test):
        if self.tracebacks:
            return super()._exc_info_to_string(err, test)
        kind, value, tb = err
        frames = traceback.extract_tb(tb)
        project_frames = [f for f in frames if "unittest" not in Path(f.filename).parts]
        frame = (project_frames or frames)[-1] if frames else None
        location = f"{Path(frame.filename).name}:{frame.lineno}" if frame else "unknown"
        message = str(value)
        # unittest discovery embeds a complete traceback inside ImportError text.
        # Preserve its root-cause line rather than echoing the wrapped traceback.
        if isinstance(value, ImportError) and "Traceback (most recent call last):" in message:
            lines = [line.strip() for line in message.splitlines() if line.strip()]
            message = lines[0][:140] + "; " + lines[-1][:260]
        return f"{compact(kind.__name__, 80)}: {compact(message)} ({location})"

    def _detail(self, label, test, message):
        self.detail_count += 1
        if self.detail_count > self.max_details:
            return
        self.stream.write(f"{label} {compact(test.id(), 220)}\n")
        self.stream.write(message + "\n")

    def addSuccess(self, test):
        super().addSuccess(test)
        self.successes += 1

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._detail("FAIL", test, self.failures[-1][1])

    def addError(self, test, err):
        super().addError(test, err)
        self._detail("ERROR", test, self.errors[-1][1])

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.bad_subtests += 1
            self._detail("SUBTEST", subtest, self._exc_info_to_string(err, subtest))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._detail(
            "UNEXPECTED_SUCCESS", test, "Remove expectedFailure or restore the expected behavior"
        )


def main(argv=None, *, stream=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="tests")
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument(
        "--tracebacks", action="store_true", help="Full local diagnostics; may contain secrets"
    )
    parser.add_argument("--max-details", type=int, default=10)
    parser.add_argument("--require-no-skips", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.max_details <= 100:
        parser.error("--max-details must be in 1..100")

    configure_logging(level=logging.WARNING)

    stream = stream if stream is not None else sys.stderr
    started = time.monotonic()
    loader = unittest.TestLoader()
    try:
        suite = loader.discover(args.start, pattern=args.pattern)
    except Exception as exc:  # noqa: BLE001 -- Discovery boundary; bounded diagnostics and nonzero exit.
        stream.write(f"DISCOVERY_ERROR {compact(type(exc).__name__)}: {compact(str(exc))}\n")
        return 2
    result = CompactResult(stream=stream, tracebacks=args.tracebacks, max_details=args.max_details)
    result.startTestRun()
    try:
        suite.run(result)
    except KeyboardInterrupt:
        stream.write("INTERRUPTED\n")
        return 130
    finally:
        result.stopTestRun()
    if result.detail_count > args.max_details:
        stream.write(f"DETAILS_OMITTED {result.detail_count - args.max_details}\n")
    # Label individual reasons, but cap their count and length.
    for test, reason in result.skipped[:5]:
        stream.write(f"SKIP {compact(test.id(), 180)}: {compact(reason, 160)}\n")
    if len(result.skipped) > 5:
        stream.write(f"SKIPS_OMITTED {len(result.skipped) - 5}\n")
    elapsed = time.monotonic() - started
    stream.write(
        f"SUMMARY run={result.testsRun} passed={result.successes} "
        f"failures={len(result.failures)} errors={len(result.errors)} "
        f"skipped={len(result.skipped)} expected_failures={len(result.expectedFailures)} "
        f"unexpected_successes={len(result.unexpectedSuccesses)} seconds={elapsed:.3f}\n"
    )
    if result.testsRun == 0:
        stream.write("NO_TESTS: discovery matched no tests\n")
        return 2
    if not result.wasSuccessful():
        return 1
    if args.require_no_skips and result.skipped:
        stream.write("SKIPS_FORBIDDEN: required checks were not executed\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
