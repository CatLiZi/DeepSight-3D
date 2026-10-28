"""Strict regression runner: a historical failure is still a failing result."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'tests')]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--original-only', action='store_true')
    args = parser.parse_args()
    original_path = PROJECT.parent / 'hardware_inventory/regression-repair-20261026/original-26.json'
    if not original_path.is_file():
        original_path = PROJECT / 'tests/fixtures/original-regression-26.json'
    original = json.loads(original_path.read_text(encoding='utf-8'))
    ids = [item['id'] for item in original]
    suite = (unittest.defaultTestLoader.loadTestsFromNames(ids) if args.original_only
             else unittest.defaultTestLoader.discover(str(PROJECT / 'tests')))
    output = io.StringIO()
    start = time.perf_counter()
    # Auto-discovery must never probe a real USB adapter in a mock-only suite.
    # Discovery tests supply their own explicit fake inventories inside this.
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), patch('locator_demo.web.app.servo_port_candidates',return_value=[]):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    issues = [{'id': test.id(), 'trace': trace} for test, trace in result.failures + result.errors]
    failed = {item['id'].split(' (')[0] for item in issues}
    report = dict(tests_run=result.testsRun, duration_s=time.perf_counter()-start,
                  failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                  successful=result.wasSuccessful(), remaining_issues=issues,
                  original_26=[dict(id=item, passed=item not in failed) for item in ids],
                  hardware_io=False,
                  source_sha256={str(p.relative_to(PROJECT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted((PROJECT/'src/locator_demo').rglob('*.py'))})
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'test-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.output / 'test-results.log').write_text(output.getvalue(), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('tests_run', 'duration_s', 'failures', 'errors', 'skipped', 'successful')}))
    return not result.wasSuccessful()


if __name__ == '__main__':
    raise SystemExit(main())
