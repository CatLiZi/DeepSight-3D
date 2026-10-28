"""Run regression tests and compare failures with the recorded pre-change baseline."""
import contextlib
import io
import json
from pathlib import Path
import sys
import time
import unittest
import warnings

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
OUT = PROJECT.parent / 'hardware_inventory/stage1'
baseline = json.loads((OUT / 'test-baseline.json').read_text(encoding='utf-8'))
known = {row['id'] for kind in ('failures', 'errors') for row in baseline[kind]}
warnings.filterwarnings('ignore', category=DeprecationWarning)
suite = unittest.defaultTestLoader.discover(str(PROJECT / 'tests'))
def test_ids(items):
    for item in items:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()
new_prefixes = ('test_native_capture.', 'test_motion.', 'test_stage1_runtime.')
new_test_count = sum(test_id.startswith(new_prefixes) for test_id in test_ids(suite))
output = io.StringIO()
started = time.perf_counter()
with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
    result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)
issues = [{'id': test.id(), 'traceback': trace} for test, trace in result.failures + result.errors]
unexpected = [row for row in issues if row['id'] not in known]
new_failures = [row for row in issues if row['id'].startswith(new_prefixes)]
report = {'tests_run': result.testsRun, 'duration_s': time.perf_counter() - started,
          'new_tests_run': new_test_count, 'new_tests_passed': new_test_count - len(new_failures),
          'suite_success': result.wasSuccessful(), 'failures': len(result.failures), 'errors': len(result.errors),
          'existing_baseline_issue_count': len(known), 'unexpected_issues': unexpected,
          'new_test_issues': new_failures, 'remaining_issues': issues,
          'resolved_baseline_issues': sorted(known - {row['id'] for row in issues})}
(OUT / 'test-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
(OUT / 'test-results.log').write_text(output.getvalue(), encoding='utf-8')
print(json.dumps({key: value for key, value in report.items() if key not in ('remaining_issues', 'unexpected_issues')}, ensure_ascii=True))
print(json.dumps({'unexpected': [row['id'] for row in unexpected]}))
raise SystemExit(bool(unexpected or new_failures))
