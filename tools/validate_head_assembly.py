"""Compare the complete suite against the pre-assembly baseline, without live hardware."""
import contextlib
import io
import json
from pathlib import Path
import sys
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
OUT = PROJECT.parent / 'hardware_inventory/stage2-head-20261023'
baseline = json.loads((OUT / 'test-baseline.json').read_text(encoding='utf-8'))
known = {r['id'] for k in ('failures', 'errors') for r in baseline[k]}
log = io.StringIO()
start = time.perf_counter()
with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
    result = unittest.TextTestRunner(stream=log).run(unittest.defaultTestLoader.discover(str(PROJECT / 'tests')))
issues = [{'id': test.id(), 'trace': trace} for test, trace in result.failures + result.errors]
report = {'tests_run': result.testsRun, 'duration_s': time.perf_counter() - start,
          'new_tests_run': result.testsRun - baseline['tests_run'],
          'failures': len(result.failures), 'errors': len(result.errors),
          'remaining_issues': issues, 'unexpected_issues': [r for r in issues if r['id'] not in known],
          'resolved': sorted(known - {r['id'] for r in issues})}
(OUT / 'test-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
(OUT / 'test-results.log').write_text(log.getvalue(), encoding='utf-8')
print(json.dumps({k: v for k, v in report.items() if k not in ('remaining_issues', 'unexpected_issues')}))
print(json.dumps({'unexpected': [r['id'] for r in report['unexpected_issues']]}))
raise SystemExit(bool(report['unexpected_issues']))
