"""v0.5: digests keep the facts an agent needs (exit code, failing names, first failure)."""
import unittest

from context_harness.shaping import digest, strip_ansi

NOISE = "\n".join(f" ✓ src/ok/file{i}.test.ts (3 tests) {i}ms" for i in range(60))

VITEST = NOISE + """
 ❯ src/components/ui/filter-dropdown.test.tsx (5 tests | 1 failed) 120ms
   × FilterDropdown > continues Tab from the trigger 40ms
     → expected <button> to have focus

 FAIL  src/components/ui/filter-dropdown.test.tsx > FilterDropdown > continues Tab from the trigger
Error: expect(element).toHaveFocus()

Expected element with focus:
  <input aria-label="Draft" />
Received element with focus:
  <button />
 ❯ src/components/ui/filter-dropdown.test.tsx:98:22

 Test Files  1 failed | 61 passed (62)
      Tests  1 failed | 187 passed (188)
   Duration  1.20s
"""

PYTEST = "\n".join(f"tests/test_mod{i}.py ........" for i in range(40)) + """
=================================== FAILURES ===================================
___________________________________ test_two ___________________________________

    def test_two():
>       assert 1 == 2
E       assert 1 == 2

tests/test_a.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_a.py::test_two - assert 1 == 2
========================= 1 failed, 321 passed in 0.03s ==========================
"""

UNITTEST = "." * 2000 + """F
======================================================================
FAIL: test_neg (tests.test_calc.T.test_neg)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_calc.py", line 7, in test_neg
    self.assertEqual(add(-1, 1), 1)
AssertionError: 0 != 1

----------------------------------------------------------------------
Ran 2001 tests in 0.010s

FAILED (failures=1)
"""

GO = "\n".join(f"=== RUN   TestOk{i}\n--- PASS: TestOk{i} (0.00s)" for i in range(60)) + """
=== RUN   TestParse
    parse_test.go:12: got 3, want 4
--- FAIL: TestParse (0.00s)
FAIL
FAIL\texample.com/pkg\t0.012s
"""

CARGO = "\n".join(f"test tests::ok_{i} ... ok" for i in range(80)) + """
test tests::parses ... FAILED

failures:

---- tests::parses stdout ----
thread 'tests::parses' panicked at src/lib.rs:10:9:
assertion `left == right` failed
  left: 3
 right: 4

test result: FAILED. 80 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""


class Shaping(unittest.TestCase):
    def test_short_output_passes_through_unchanged(self):
        view = digest("Ran 2 tests\n\nOK\n", exit_code=0)
        self.assertFalse(view["shaped"])
        self.assertEqual(view["output"], "Ran 2 tests\n\nOK\n")
        self.assertEqual(view["exit_code"], 0)

    def test_ansi_is_stripped_before_measuring(self):
        self.assertEqual(strip_ansi("\x1b[31mFAIL\x1b[0m x"), "FAIL x")

    def test_vitest(self):
        view = digest(VITEST, exit_code=1)
        self.assertTrue(view["shaped"])
        self.assertEqual(view["counts"]["failed"], 1)
        self.assertEqual(view["counts"]["passed"], 187)
        self.assertEqual(view["counts"]["total"], 188)
        self.assertEqual(view["failed_tests"], [
            "src/components/ui/filter-dropdown.test.tsx > FilterDropdown > continues Tab from the trigger"])
        self.assertIn("toHaveFocus", view["first_failure"]["text"])
        self.assertIn("Received element with focus", view["first_failure"]["text"])
        self.assertLess(len(str(view)), len(VITEST))

    def test_pytest(self):
        view = digest(PYTEST, exit_code=1)
        self.assertEqual(view["counts"]["failed"], 1)
        self.assertEqual(view["counts"]["passed"], 321)
        self.assertEqual(view["failed_tests"], ["tests/test_a.py::test_two"])
        self.assertIn("assert 1 == 2", view["first_failure"]["text"])

    def test_unittest(self):
        view = digest(UNITTEST, exit_code=1)
        self.assertEqual(view["counts"]["total"], 2001)
        self.assertEqual(view["counts"]["failures"], 1)
        self.assertEqual(view["failed_tests"], ["test_neg (tests.test_calc.T.test_neg)"])
        self.assertIn("AssertionError: 0 != 1", view["first_failure"]["text"])

    def test_go_and_cargo(self):
        go = digest(GO, exit_code=1)
        self.assertEqual(go["failed_tests"], ["TestParse"])
        self.assertIn("got 3, want 4", go["first_failure"]["text"])
        cargo = digest(CARGO, exit_code=101)
        self.assertEqual(cargo["counts"]["failed"], 1)
        self.assertEqual(cargo["counts"]["passed"], 80)
        self.assertEqual(cargo["failed_tests"], ["tests::parses"])

    def test_passing_long_output_has_no_failure_excerpt(self):
        view = digest(NOISE + "\n Tests  188 passed (188)\n", exit_code=0)
        self.assertNotIn("first_failure", view)
        self.assertEqual(view["counts"], {"passed": 188, "total": 188, "runner": "vitest"})


if __name__ == "__main__":
    unittest.main()
