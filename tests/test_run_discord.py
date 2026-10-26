import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('local_launcher', Path(__file__).resolve().parents[1] / 'scripts/run_discord.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LocalLauncherTests(unittest.TestCase):
    def test_reads_settings_as_data_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / '.env.local'
            path.write_text("DISCORD_BOT_TOKEN='synthetic.value'\nDISCORD_ALLOWED_USERS=123\nMAILBOT_IMAGE_DIR='$(touch nope)'\n")
            path.chmod(0o600)
            settings = launcher.read_settings(path)
            self.assertEqual(settings['MAILBOT_IMAGE_DIR'], '$(touch nope)')
            self.assertEqual(settings['DISCORD_ALLOWED_USERS'], '123')

    def test_rejects_symlink_and_open_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'private'
            path.write_text('DISCORD_ALLOWED_USERS=123\n')
            path.chmod(0o644)
            with self.assertRaises(ValueError): launcher.read_settings(path)
            path.chmod(0o600)
            link = Path(d) / 'link'
            link.symlink_to(path)
            with self.assertRaises(ValueError): launcher.read_settings(link)
