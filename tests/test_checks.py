"""Exercise deterministic checks and the actual environment of Git children."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_status import status


class CheckTests(unittest.TestCase):
    def test_git_child_has_no_authority_or_git_routing(self):
        # Probe the actual subprocess boundary without teaching the core to run commands.
        run = subprocess.run

        def probe(command, **kwargs):
            result = run([sys.executable, '-c',
                          'import json, os; print(json.dumps(dict(os.environ)))'], **kwargs)
            environment = json.loads(result.stdout)
            self.assertNotIn('PROJECT_COMMIT_LEASE', environment)
            self.assertNotIn('commit_nonce', environment)
            self.assertNotIn('GIT_DIR', environment)
            self.assertNotIn('GIT_CONFIG_COUNT', environment)
            self.assertNotIn('PROJECT_COMMIT_AUTH', environment)
            return result

        with patch.dict(os.environ, {'PROJECT_COMMIT_LEASE': 'test', 'commit_nonce': 'test',
                                     'GIT_DIR': '/invalid', 'GIT_CONFIG_COUNT': '999',
                                     'PROJECT_COMMIT_AUTH': 'test'}):
            with patch.object(status.subprocess, 'run', side_effect=probe):
                status.git(Path.cwd(), 'rev-parse', 'HEAD')

    def test_child_environment_rejects_empty_and_mixed_case_authority(self):
        for key in ('LEASE', 'project_lease_id', 'Commit_NoNcE'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                status.check_child_env({key: ''})
        status.check_child_env({'PATH': '/usr/bin'})

    def test_known_gaps_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'KNOWN-GAPS.md'
            for count in (0, 30, 31):
                path.write_text('# Known gaps\n\n' + '- gap: impact\n' * count)
                if count <= 30:
                    status.check_known_gaps(path)
                else:
                    with self.assertRaisesRegex(ValueError, 'cap of 30'):
                        status.check_known_gaps(path)

    def test_known_gaps_cannot_hide_entries_in_other_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'KNOWN-GAPS.md'
            for body in ('', '# Other\n', '# Known gaps\n- \n',
                         '# Known gaps\n* hidden\n', '# Known gaps\n- gap\n  continuation\n'):
                path.write_text(body)
                with self.subTest(body=body), self.assertRaises(ValueError):
                    status.check_known_gaps(path)

    def test_cli_verdicts_and_missing_list(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'KNOWN-GAPS.md'
            command = [sys.executable, status.__file__, '--known-gaps', str(path)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout), (1, 'fail\n'))
            path.write_text('# Known gaps\n')
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout), (0, 'pass\n'))

    def test_cli_checks_its_own_child_environment(self):
        command = [sys.executable, status.__file__, '--check-child-env']
        clean = status.git_environment()
        for environment, expected in ((clean, (0, 'pass\n')),
                                      ({**clean, 'PROJECT_NONCE': ''}, (1, 'fail\n'))):
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout), expected)

    def test_release_metadata_is_not_authority(self):
        environment = {**status.git_environment(), 'RELEASE_CHANNEL': 'stable',
                       'RELEASE_VERSION': '0.2.1'}
        status.check_child_env(environment)
        command = [sys.executable, status.__file__, '--check-child-env']
        result = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout), (0, 'pass\n'))
        for key in ('LEASE', 'PROJECT_COMMIT_LEASE', 'commit_nonce', 'Commit_NoNcE',
                    'project-lease-id', 'NONCE'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                status.check_child_env({**environment, key: ''})
