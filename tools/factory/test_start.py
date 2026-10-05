"""Launcher preflight tests: no BAND credentials or workers are used."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).with_name('start.sh')
BASH = r'C:\Program Files\Git\bin\bash.exe' if os.name == 'nt' else shutil.which('bash')

class StartTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run([BASH, SCRIPT.as_posix(), *map(str, args)], capture_output=True, text=True, timeout=10)

    def test_help_requires_no_credentials(self):
        result = self.run_script('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Usage:', result.stdout)

    def test_missing_arguments_explain_usage(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertIn('Usage:', result.stderr)

    def test_incomplete_mandates_abort_before_credentials(self):
        with tempfile.TemporaryDirectory(prefix='factory paths ') as tmp:
            root = pathlib.Path(tmp)
            mandates = root / 'mandates'
            repo = root / 'result'
            mandates.mkdir()
            repo.mkdir()
            for seat in ('coordinator', 'implementer'):
                (mandates / (seat + '.md')).write_text('A mandate', encoding='utf-8')
            result = self.run_script(root.as_posix(), repo.as_posix())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Missing mandate:', result.stderr)
            self.assertIn('reviewer.md', result.stderr)
            self.assertNotIn('pid ', result.stdout)

if __name__ == '__main__':
    unittest.main()
