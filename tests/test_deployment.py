"""Stage real archives locally; never connect to a machine or run GPU tests."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hitest_deployer', PROJECT / 'deploy/deploy.py')
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)
PLATFORM = PROJECT.parents[1] / 'hygon-dcu-test-platform'
if (PLATFORM / 'backend/tests/support.py').is_file():
    sys.path[:0] = [str(PLATFORM / 'backend/src'), str(PLATFORM / 'backend/tests')]
    from support import Harness, LocalTransport
    from dcu_platform.modules.execution.transport import CommandResult
else:
    Harness = None


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hitest-deploy-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive, self.digest, self.manifest = deployer.package(PROJECT, self.root / 'bundle')
        self.base = self.root / 'remote root with spaces' / 'tools/HiTest'

    def install(self, archive=None, digest=None):
        return subprocess.run([sys.executable, '-c', deployer.INSTALL, str(archive or self.archive), str(self.base), digest or self.digest], capture_output=True, text=True, timeout=15)

    def test_bundle_has_scripts_and_tests_but_no_credentials_git_or_logs(self):
        with tarfile.open(self.archive) as tar:
            names = tar.getnames()
            self.assertIn('scripts/nvidia-load-unload.sh', names)
            self.assertIn('hitest', names)
            self.assertTrue(all(not name.startswith(('logs/', '.git/', 'data/')) for name in names))
            self.assertNotIn('credentials', ' '.join(names))
        second, digest, manifest = deployer.package(PROJECT, self.root / 'second')
        self.assertEqual(self.manifest, manifest)
        self.assertEqual(self.digest, digest)
        self.assertEqual(self.archive.read_bytes(), second.read_bytes())

    def test_verified_install_is_executable_idempotent_and_preserves_shared_logs(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['state'], 'deployed')
        current = self.base / 'current'
        self.assertTrue(current.is_symlink())
        self.assertEqual((current / 'hitest').stat().st_mode & 0o777, 0o755)
        self.assertTrue(os.access(current / 'hitest', os.X_OK))
        log = self.base / 'logs/previous-test.log'
        log.write_text('keep existing logs')
        self.assertEqual((current / 'logs/previous-test.log').read_text(), 'keep existing logs')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text(), 'keep existing logs')
        self.assertEqual(list((self.base / 'releases').glob('.stage-*')), [])
        # List only: this should never create GPU test logs.
        listed = subprocess.run([str(current / 'hitest'), 'list'], capture_output=True, text=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn('nvidia-load-unload', listed.stdout)
        self.assertEqual([p.name for p in (self.base / 'logs').iterdir()], ['previous-test.log'])

    def test_bad_archive_checksum_does_not_switch_current(self):
        self.assertNotEqual(self.install(digest='0' * 64).returncode, 0)
        self.assertFalse((self.base / 'current').exists())

    def test_tar_path_escape_is_rejected_without_writing_outside_target(self):
        archive = self.root / 'escape.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            member = tarfile.TarInfo('../../escaped')
            member.size = 4
            tar.addfile(member, io.BytesIO(b'test'))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertNotEqual(self.install(archive, digest).returncode, 0)
        self.assertFalse((self.root / 'escaped').exists())
        self.assertFalse((self.base / 'current').exists())

    def test_tar_symlink_is_rejected(self):
        archive = self.root / 'link.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            member = tarfile.TarInfo('hitest')
            member.type = tarfile.SYMTYPE
            member.linkname = '/etc/passwd'
            tar.addfile(member)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertNotEqual(self.install(archive, digest).returncode, 0)
        self.assertFalse((self.base / 'current').exists())

    def test_changed_existing_release_is_not_reused_as_verified(self):
        self.assertEqual(self.install().returncode, 0)
        current = self.base / 'current'
        previous = os.readlink(current)
        (current / 'hitest').write_text('#!/bin/bash\nexit 0\n')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Existing release was modified', result.stderr)
        self.assertEqual(os.readlink(current), previous)

    def harness(self):
        harness = Harness()
        self.addCleanup(harness.close)
        env = harness.app.environments.create({'name': 'deployment fixture', 'hostname': 'fixture.invalid', 'remote_root': str(self.root / 'dut')}, harness.admin)
        return harness, env

    @unittest.skipUnless(Harness, 'Sibling platform is not available; archive verification still runs')
    def test_real_stage_and_platform_reservation_are_released_after_success(self):
        harness, env = self.harness()
        class Adapter(LocalTransport):
            def run(self, command, timeout=30):
                if 'sys.version_info' in command[2]: return CommandResult(0, '{"uid":0,"python_ok":true}')
                return super().run(command, timeout)
        transport = Adapter()
        result = deployer.deploy(harness.app, env, harness.admin, self.archive, self.digest, self.manifest, lambda *_: transport)
        self.assertEqual(result['state'], 'deployed')
        self.assertEqual(harness.app.environments.get(env['id'])['state'], 'idle')
        with harness.app.db.transaction() as conn:
            events = conn.all("SELECT * FROM audit_events WHERE action='tool.hitest.deploy'")
        self.assertEqual(len(events), 1)
        self.assertEqual(list((Path(env['remote_root']) / '.hitest-uploads').iterdir()), [])

    @unittest.skipUnless(Harness, 'Sibling platform is not available; archive verification still runs')
    def test_failed_ssh_preserves_failure_and_releases_only_its_reservation(self):
        harness, env = self.harness()
        class Offline:
            def run(self, command, timeout=30): return CommandResult(255, '', 'fixture connection failed')
        with self.assertRaisesRegex(RuntimeError, 'fixture connection failed'):
            deployer.deploy(harness.app, env, harness.admin, self.archive, self.digest, self.manifest, lambda *_: Offline())
        self.assertEqual(harness.app.environments.get(env['id'])['state'], 'idle')
        with harness.app.db.transaction() as conn:
            self.assertFalse(conn.all("SELECT * FROM audit_events WHERE action='tool.hitest.deploy'"))

    @unittest.skipUnless(Harness, 'Sibling platform is not available; archive verification still runs')
    def test_occupied_device_never_opens_ssh_and_keeps_its_owner(self):
        harness, env = self.harness()
        lease = harness.app.reservations.create({'environment_id': env['id'], 'minutes': 5, 'reason': 'existing work'}, harness.admin)
        opened = []
        def factory(*_): opened.append(True)
        with self.assertRaises(Exception):
            deployer.deploy(harness.app, env, harness.admin, self.archive, self.digest, self.manifest, factory)
        self.assertEqual(opened, [])
        self.assertEqual(harness.app.environments.get(env['id'])['owner_reservation_id'], lease['id'])


if __name__ == '__main__': unittest.main()
