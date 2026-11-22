"""Exercise the shell installer without GitHub, credentials, or a live gateway."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'install-sendblue-one-shot.sh'


class InstallerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'calls'
        self.env = dict(HOME=tmp.name, HERMES_HOME=str(self.root / 'data'),
                        PATH=f'{self.bin}{os.pathsep}{os.defpath}', TMPDIR=tmp.name,
                        CALLS=str(self.calls), SENDBLUE_INSTALLER_RESTART='1')
        self.executable('git', '''case "$1" in
clone) mkdir -p "$5"; touch "$5/adapter.py" "$5/plugin.yaml" "$5/__init__.py";;
-C) printf 'fixture\\n';;
*) exit 88;;
esac''')
        self.executable('hermes', 'echo fallback >> "$CALLS"; exit 9')
        self.chosen = self.executable('chosen hermes', '''echo "$*" >> "$CALLS"
case "$1 $2" in
'plugins enable') echo 'enable diagnostic'; exit "${ENABLE_RC:-0}";;
'plugins list') printf '%s\\n' "$LIST"; exit "${LIST_RC:-0}";;
'gateway restart') exit 0;;
*) exit 90;;
esac''')
        self.env.update(HERMES_BIN=str(self.chosen),
                        LIST='[{"name":"sendblue-platform","status":"enabled"}]')

    def executable(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\nset -eu\n' + body + '\n')
        path.chmod(0o700)
        return path

    def install(self, **env):
        result = subprocess.run(['bash', str(SCRIPT)], env=self.env | env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        calls = self.calls.read_text() if self.calls.exists() else ''
        return result, calls

    def test_invalid_override_stops_before_copy(self):
        result, calls = self.install(HERMES_BIN=str(self.bin / 'missing'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(calls)
        self.assertFalse((self.root / 'data/plugins/sendblue').exists())

    def test_override_with_spaces_is_used(self):
        result, calls = self.install(SENDBLUE_INSTALLER_RESTART='0')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('plugins enable sendblue-platform', calls)
        self.assertNotIn('fallback', calls)
        self.assertNotIn('gateway restart', calls)
        self.assertIn('Installed and enabled; gateway not restarted', result.stdout)

    def test_enable_failure_surfaces_error_before_restart(self):
        result, calls = self.install(ENABLE_RC='7')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('enable diagnostic', result.stdout)
        self.assertIn('Installed, but not enabled', result.stdout)
        self.assertNotIn('gateway restart', calls)

    def test_unconfirmed_enable_blocks_even_forced_restart(self):
        for listing, rc in [('[]', '0'), ('not JSON', '0'), (self.env['LIST'], '8'),
                            ('[{"name":"sendblue-platform","status":"disabled"}]', '0')]:
            with self.subTest(listing=listing, rc=rc):
                self.calls.unlink(missing_ok=True)
                result, calls = self.install(LIST=listing, LIST_RC=rc)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('not confirmed', result.stdout)
                self.assertNotIn('gateway restart', calls)

    def test_verified_enable_precedes_requested_restart(self):
        result, calls = self.install()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertLess(calls.index('plugins list'), calls.index('gateway restart'))

    def test_home_channel_defaults_to_first_allowed_user_only(self):
        result, calls = self.install(
            SENDBLUE_INSTALLER_RESTART='0',
            SENDBLUE_ALLOWED_USERS='+15550001,+15550002')
        self.assertEqual(result.returncode, 0, result.stdout)
        env_text = (self.root / 'data' / '.env').read_text()
        self.assertIn('SENDBLUE_ALLOWED_USERS=+15550001,+15550002\n', env_text)
        self.assertIn('SENDBLUE_HOME_CHANNEL=+15550001\n', env_text)
        self.assertNotIn('SENDBLUE_HOME_CHANNEL=+15550001,+15550002', env_text)

    def test_env_append_keeps_existing_last_line_intact(self):
        data = self.root / 'data'
        data.mkdir(parents=True)
        (data / '.env').write_text('EXISTING=1')  # no trailing newline
        result, calls = self.install(SENDBLUE_INSTALLER_RESTART='0',
                                     SENDBLUE_API_KEY='test-key')
        self.assertEqual(result.returncode, 0, result.stdout)
        env_text = (data / '.env').read_text()
        self.assertIn('EXISTING=1\n', env_text)
        self.assertIn('\nSENDBLUE_API_KEY=test-key\n', env_text)
