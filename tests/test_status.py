"""Real Git snapshots exercise the pin boundary without running consumer commands."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    'loop_status', Path(__file__).resolve().parents[1] / 'core/tools/status.py'
)
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / 'source'
        self.source.mkdir()
        self.consumer = root / 'consumer'
        self.consumer.mkdir()
        self.run_git('init', '-q')
        self.run_git('config', 'user.name', 'Test')
        self.run_git('config', 'user.email', 'test@example.invalid')
        (self.source / 'core').mkdir()
        (self.source / 'core/contract.md').write_text('shared contract\n')
        self.run_git('add', 'core')
        self.run_git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture')
        self.revision = self.run_git('rev-parse', 'HEAD').strip()
        self.vendor = self.consumer / 'vendor/loop-zero'
        shutil.copytree(self.source / 'core', self.vendor)
        self.write_contract()

    def run_git(self, *args):
        return subprocess.run(['git', '-C', str(self.source), *args],
                              capture_output=True, text=True, check=True).stdout

    def write_contract(self, revision=None, path='vendor/loop-zero'):
        (self.consumer / 'workflow.toml').write_text(
            f'[core]\nrevision = "{revision or self.revision}"\npath = "{path}"\n'
            '[commands]\nsetup = "touch MUST_NOT_EXECUTE"\n'
        )

    def test_matching_snapshot_repeat_is_read_only(self):
        before = {p: p.read_bytes() for p in self.consumer.rglob('*') if p.is_file()}
        for _ in range(2):
            self.assertEqual(status.verify(self.consumer, self.source), self.revision)
        after = {p: p.read_bytes() for p in self.consumer.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.consumer / 'MUST_NOT_EXECUTE').exists())

    def test_added_missing_and_modified_files_fail(self):
        for change in ('added', 'missing', 'changed'):
            with self.subTest(change=change):
                shutil.rmtree(self.vendor)
                shutil.copytree(self.source / 'core', self.vendor)
                if change == 'added':
                    (self.vendor / 'extra').write_text('extra')
                elif change == 'missing':
                    (self.vendor / 'contract.md').unlink()
                else:
                    (self.vendor / 'contract.md').write_text('local fork')
                with self.assertRaisesRegex(ValueError, 'differs from pin'):
                    status.verify(self.consumer, self.source)

    def test_branch_and_absent_commit_fail(self):
        for revision in ('HEAD', self.revision[:12], '0' * 40):
            with self.subTest(revision=revision):
                self.write_contract(revision=revision)
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    status.verify(self.consumer, self.source)

    def test_another_commit_cannot_certify_old_files(self):
        (self.source / 'core/contract.md').write_text('new contract')
        self.run_git('add', 'core')
        self.run_git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'changed')
        self.write_contract(revision=self.run_git('rev-parse', 'HEAD').strip())
        with self.assertRaisesRegex(ValueError, 'differs from pin'):
            status.verify(self.consumer, self.source)

    def test_replacement_ref_cannot_substitute_pinned_bytes(self):
        (self.source / 'core/contract.md').write_text('substituted contract')
        self.run_git('add', 'core')
        self.run_git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'replacement')
        replacement = self.run_git('rev-parse', 'HEAD').strip()
        self.run_git('replace', self.revision, replacement)
        self.assertEqual(status.verify(self.consumer, self.source), self.revision)
        (self.vendor / 'contract.md').write_text('substituted contract')
        with self.assertRaisesRegex(ValueError, 'differs from pin'):
            status.verify(self.consumer, self.source)

    def test_blob_pin_fails(self):
        blob = self.run_git('rev-parse', 'HEAD:core/contract.md').strip()
        self.write_contract(revision=blob)
        with self.assertRaisesRegex(ValueError, 'identify a commit'):
            status.verify(self.consumer, self.source)

    def test_traversal_and_absolute_paths_fail(self):
        for path in ('../outside', str(self.vendor), '.'):
            with self.subTest(path=path):
                self.write_contract(path=path)
                with self.assertRaises(ValueError):
                    status.verify(self.consumer, self.source)

    def test_symlinked_parent_fails(self):
        outside = self.consumer.parent / 'outside'
        (self.consumer / 'vendor').rename(outside)
        (self.consumer / 'vendor').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            status.verify(self.consumer, self.source)

    def test_vendored_file_and_directory_symlinks_fail(self):
        for target in (self.source / 'core/contract.md', self.source / 'core'):
            with self.subTest(target=target):
                link = self.vendor / 'external'
                link.symlink_to(target, target_is_directory=target.is_dir())
                with self.assertRaisesRegex(ValueError, 'symlinks'):
                    status.verify(self.consumer, self.source)
                link.unlink()

    def test_source_symlink_is_not_a_portable_file(self):
        (self.source / 'core/link').symlink_to('contract.md')
        self.run_git('add', 'core')
        self.run_git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'symlink')
        self.write_contract(revision=self.run_git('rev-parse', 'HEAD').strip())
        with self.assertRaisesRegex(ValueError, 'ordinary files'):
            status.verify(self.consumer, self.source)

    def test_cli_missing_source_is_nonzero(self):
        result = subprocess.run(
            [sys.executable, str(Path(status.__file__)), '--consumer', str(self.consumer),
             '--source', str(self.consumer / 'missing')], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('UNVERIFIED', result.stderr)


if __name__ == '__main__':
    unittest.main()
