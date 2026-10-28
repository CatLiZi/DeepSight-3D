"""Run regression checks and compare against the recorded pre-binocular issues."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--new-pattern",default="test_binocular.py")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    baseline = json.loads((PROJECT.parent / "hardware_inventory/focus-upgrade-20261024/test-results.json").read_text(encoding="utf-8"))
    expected = {issue["id"] for issue in baseline["remaining_issues"]}
    suite = unittest.defaultTestLoader.discover(str(PROJECT / "tests"))
    new_suite = unittest.defaultTestLoader.discover(str(PROJECT / "tests"), pattern=args.new_pattern)
    new_count = new_suite.countTestCases()
    def identifiers(items):
        result=set()
        for item in items:
            if isinstance(item,unittest.TestSuite): result.update(identifiers(item))
            else: result.add(item.id())
        return result
    new_ids=identifiers(new_suite)
    output = io.StringIO()
    start = time.perf_counter()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    issues = [{"id": test.id(), "trace": trace} for test, trace in result.failures+result.errors]
    ids = {issue["id"] for issue in issues}
    report = {"tests_run": result.testsRun, "duration_s": time.perf_counter()-start,
              "failures": len(result.failures), "errors": len(result.errors),
              "new_tests_run": new_count,
              "new_tests_passed": new_count-sum(issue["id"] in new_ids for issue in issues),
              "new_test_pattern":args.new_pattern,
              "baseline": "hardware_inventory/focus-upgrade-20261024/test-results.json",
              "remaining_issues": issues, "unexpected_issues": sorted(ids-expected),
              "resolved_previous_issues": sorted(expected-ids), "hardware_io": False}
    (args.output / "test-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "test-results.log").write_text(output.getvalue(), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k != "remaining_issues"}))
    return bool(report["unexpected_issues"])


if __name__ == "__main__":
    raise SystemExit(main())
