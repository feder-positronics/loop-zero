"""Offline policy fixtures: reporting cannot execute consumer commands."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

spec = importlib.util.spec_from_file_location(
    'check_policy', Path(__file__).resolve().parents[1] / 'core/tools/checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
FIXTURES = Path(__file__).parent / 'fixtures'


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = tomllib.loads((FIXTURES / 'workflow.toml').read_text())['checks']
        self.results = json.loads((FIXTURES / 'check-results.json').read_text())

    def test_fixture_classes_and_blockers(self):
        rows = {r['name']: r for r in checks.classify(self.config, self.results)}
        self.assertEqual([rows[n]['class'] for n in self.config['required']], [1, 1, 3])
        self.assertEqual(rows['types']['action'], 'would block PR')
        self.assertEqual(rows['external-links']['class'], 2)
        for name in ('lint', 'optional-tests', 'external-links'):
            self.assertEqual(rows[name]['action'], 'does not block PR')
        self.assertIn('report infrastructure; no code verdict', rows['tests']['action'])
        self.assertIn('required evidence unavailable', rows['tests']['action'])
        self.assertEqual(rows['dependency-audit']['action'], 'retry once automatically')
        self.assertEqual({n for n, r in rows.items() if r['blocks']}, {'types', 'tests'})
        self.assertTrue(rows['tests']['action'].endswith('; would block PR'))

    def test_missing_required_evidence_blocks(self):
        rows = checks.classify(self.config, {})
        self.assertEqual([r['name'] for r in rows if r['blocks']], self.config['required'])
        for row in rows:
            self.assertEqual(row['action'], 'would block PR; no result yet'
                             if row['blocks'] else 'does not block PR')

    def test_required_infrastructure_loss_blocks_at_every_attempt(self):
        for reason in checks.INFRASTRUCTURE:
            for attempts in (1, 2):
                with self.subTest(reason=reason, attempts=attempts):
                    row = checks.classify(self.config, {'lint': {
                        'conclusion': reason, 'attempts': attempts}})[0]
                    self.assertEqual((row['class'], row['blocks']), (3, True))
                    self.assertTrue(row['action'].endswith(
                        'required evidence unavailable; would block PR'))
        row = checks.classify(self.config, {'optional-tests': {'conclusion': 'runner_loss'}})
        self.assertEqual([(r['class'], r['blocks'], r['action']) for r in row
                          if r['name'] == 'optional-tests'],
                         [(3, False, 'retry once automatically')])

    def test_each_infrastructure_reason_retries_only_once(self):
        for reason in checks.INFRASTRUCTURE:
            for attempts in (1, 2, 3):
                with self.subTest(reason=reason, attempts=attempts):
                    row = checks.classify(self.config, {'lint': {
                        'conclusion': reason, 'attempts': attempts}})[0]
                    self.assertEqual(row['class'], 3)
                    self.assertIn('retry once' if attempts == 1 else 'report infrastructure',
                                  row['action'])

    def test_invalid_policy_and_results_fail_closed(self):
        for policy in ({}, {**self.config, 'required': 'lint'},
                       {**self.config, 'scheduled': ['lint']},
                       {**self.config, 'required': ['']},
                       {**self.config, 'required': ['lint', 'lint']}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                checks.classify(policy, {})
        for results in ([], {'unknown': {'conclusion': 'success'}},
                        {'lint': {'conclusion': 'cancelled'}},
                        {'lint': {'conclusion': 'runner_loss', 'attempts': True}},
                        {'lint': {'conclusion': 'failure', 'attempts': 0}}):
            with self.subTest(results=results), self.assertRaises(ValueError):
                checks.classify(self.config, results)

    def test_cli_is_read_only_and_does_not_execute_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('workflow.toml', 'check-results.json'):
                (root / name).write_bytes((FIXTURES / name).read_bytes())
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            command = [sys.executable, checks.__file__, 'checks', '--results',
                       'check-results.json']
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), checks.classify(self.config, self.results))
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
            for bad in ('[checks]\nrequired = "bad"\n', '[commands]\ncheck_fast = "x"\n'):
                (root / 'workflow.toml').write_text(bad)
                result = subprocess.run(command, cwd=root, capture_output=True, text=True)
                self.assertEqual((result.returncode, result.stdout), (1, ''))
                self.assertIn('UNVERIFIED', result.stderr)
            self.assertIn('no [checks] table', result.stderr)
            (root / 'check-results.json').write_text('[' * 100000 + ']' * 100000)
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout), (1, ''))
            self.assertIn('UNVERIFIED', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
