import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CompactRunnerTests(unittest.TestCase):
    def run_source(self, source, *flags):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "test_fixture.py").write_text(source)
            return subprocess.run(
                [sys.executable, "-m", "zvonilka.testing.runner", "--start", temp, *flags],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            )

    def test_success(self):
        r = self.run_source("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(2+2,4)\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("passed=1", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_assertion_failure_nonzero_without_traceback(self):
        r = self.run_source("import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.assertEqual('actual','expected')\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("AssertionError", r.stderr)
        self.assertIn("actual", r.stderr)
        self.assertIn("expected", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("failures=1", r.stderr)

    def test_unhandled_exception_nonzero(self):
        r = self.run_source("import unittest\nclass T(unittest.TestCase):\n def test_bad(self): raise RuntimeError('dependency unavailable')\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("RuntimeError: dependency unavailable", r.stderr)
        self.assertIn("errors=1", r.stderr)

    def test_import_error_nonzero(self):
        r = self.run_source("import definitely_missing_zvonilka_module\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ModuleNotFoundError", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_setup_class_failure_nonzero(self):
        r = self.run_source("import unittest\nclass T(unittest.TestCase):\n @classmethod\n def setUpClass(cls): raise RuntimeError('setup failed')\n def test_ok(self): pass\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("setup failed", r.stderr)
        self.assertIn("errors=1", r.stderr)

    def test_skips_not_counted_as_passes(self):
        source = "import unittest\nclass T(unittest.TestCase):\n @unittest.skip('Redis unavailable')\n def test_skip(self): pass\n"
        r = self.run_source(source)
        self.assertEqual(r.returncode, 0)
        self.assertIn("skipped=1", r.stderr)
        self.assertIn("passed=0", r.stderr)
        r = self.run_source(source, "--require-no-skips")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SKIPS_FORBIDDEN", r.stderr)

    def test_empty_discovery_is_error(self):
        r = self.run_source("# no tests\n")
        self.assertEqual(r.returncode, 2)
        self.assertIn("NO_TESTS", r.stderr)

    def test_full_traceback_requires_flag(self):
        r = self.run_source("import unittest\nclass T(unittest.TestCase):\n def test_bad(self): raise ValueError('broken')\n", "--tracebacks")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Traceback", r.stderr)

    def test_failure_details_bounded(self):
        source = "import unittest\nclass T(unittest.TestCase):\n" + "".join(
            f" def test_{i}(self): self.fail('" + "x"*5000 + "')\n" for i in range(20))
        r = self.run_source(source, "--max-details", "2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("failures=20", r.stderr)
        self.assertIn("DETAILS_OMITTED 18", r.stderr)
        self.assertLess(len(r.stderr), 2000)

    def test_subtest_failure_is_not_success(self):
        r = self.run_source("import unittest\nclass T(unittest.TestCase):\n def test_sub(self):\n  with self.subTest(value=1): self.assertEqual(1,2)\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SUBTEST", r.stderr)
        self.assertIn("passed=0", r.stderr)

    def test_unexpected_success_fails(self):
        r = self.run_source("import unittest\nclass T(unittest.TestCase):\n @unittest.expectedFailure\n def test_unexpected(self): pass\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unexpected_successes=1", r.stderr)

    def test_control_characters_escaped(self):
        r = self.run_source("import unittest\nclass T(unittest.TestCase):\n def test_bad(self): raise ValueError('hello\\nworld\\x1b[31m')\n")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("\x1b", r.stderr)
        self.assertIn("hello", r.stderr)
