"""Execute real Bash/entry/signals against temporary fake sysfs and GPU commands."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class NvidiaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hitest-nvidia-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'HiTest with spaces'
        (self.project / 'scripts').mkdir(parents=True)
        shutil.copy2(PROJECT / 'hitest', self.project / 'hitest')
        self.modules = self.root / 'modules'
        self.modules.mkdir()
        self.devices = self.root / 'dev'
        self.devices.mkdir()
        (self.devices / 'nvidia0').touch()
        self.pci = self.root / 'pci'
        gpu = self.pci / '0000:01:00.0'
        gpu.mkdir(parents=True)
        (gpu / 'vendor').write_text('0x10de\n')
        (gpu / 'class').write_text('0x030200\n')
        self.drm = self.root / 'drm'
        self.drm.mkdir()
        source = (PROJECT / 'scripts/nvidia-load-unload.sh').read_text()
        # Testing adapter only: production never accepts alternative sysfs paths.
        source = source.replace('if (( EUID != 0 )); then', 'if false; then')
        for key, value in {'MODULE_ROOT=/sys/module': self.modules, 'PCI_ROOT=/sys/bus/pci/devices': self.pci,
                           'DRM_ROOT=/sys/class/drm': self.drm, 'DEVICE_ROOT=/dev': self.devices,
                           'LOCK_FILE=/run/hitest/nvidia-load-unload.lock': self.root / 'test.lock'}.items():
            source = source.replace(key, key.split('=')[0] + "='" + str(value) + "'")
        (self.project / 'scripts/nvidia-load-unload.sh').write_text(source)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.mock('dmesg', '#!/bin/bash\necho mock-kernel-log\n')
        self.mock('fuser', '#!/bin/bash\nif [[ "${MOCK_BUSY:-0}" == 1 ]]; then echo 1234; exit 0; fi\nexit 1\n')
        self.mock('modinfo', '''#!/bin/bash
if [[ "$1" == -F ]]; then echo fixture-driver-version; exit 0; fi
if [[ "${MOCK_MISSING:-}" == "$1" ]]; then exit 1; fi
if [[ "${MOCK_HEADLESS:-0}" == 1 && ( "$1" == nvidia_modeset || "$1" == nvidia_drm ) ]]; then exit 1; fi
exit 0
''')
        self.mock('modprobe', '''#!/usr/bin/env python3
import os,sys,time
from pathlib import Path
operation='unload' if sys.argv[1]=='-r' else 'load'
name=sys.argv[-1]
print('BEGIN '+operation+' '+name,flush=True)
time.sleep(0.06)
if os.environ.get('MOCK_FAIL')==operation+':'+name: sys.exit(7)
module=Path(os.environ['MOCK_MODULE_ROOT'])/name
if operation=='load':
    module.mkdir(exist_ok=True)
    (module/'holders').mkdir(exist_ok=True)
    if name=='nvidia':
        rounds=Path(os.environ['MOCK_ROUNDS'])
        rounds.write_text(str(int(rounds.read_text())+1 if rounds.exists() else 1))
elif module.exists() and os.environ.get('MOCK_KEEP')!=name:
    for holder in (module/'holders').iterdir(): holder.unlink()
    (module/'holders').rmdir()
    module.rmdir()
print('END '+operation+' '+name,flush=True)
''')
        self.mock('nvidia-smi', '''#!/usr/bin/env python3
import os,sys
from pathlib import Path
if not (Path(os.environ['MOCK_MODULE_ROOT'])/'nvidia').is_dir(): sys.exit(8)
if os.environ.get('MOCK_SMI_FAIL')=='1': sys.exit(9)
if os.environ.get('MOCK_SMI_EMPTY')=='1': sys.exit(0)
uuid='GPU-fixture'
if os.environ.get('MOCK_CHANGE')=='1' and int(Path(os.environ['MOCK_ROUNDS']).read_text())>1: uuid='GPU-changed'
print(uuid+', fixture GPU, fixture-driver-version' if 'uuid,name' in sys.argv[1] else uuid)
''')
        self.mock('sleep', '#!/usr/bin/env python3\nimport time\ntime.sleep(0.01)\n')
        self.output = self.root / 'console.txt'
        self.processes = []
        self.addCleanup(self.cleanup_processes)

    def mock(self, name, source):
        target = self.bin / name
        target.write_text(source)
        target.chmod(0o755)

    def loaded(self, name):
        (self.modules / name / 'holders').mkdir(parents=True)

    def start(self, args=('1', '0'), **extra):
        env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'], MOCK_MODULE_ROOT=str(self.modules), MOCK_ROUNDS=str(self.root / 'rounds'), **extra)
        for key in ('HITEST_RUN_DIR', 'HITEST_LOG_ROOT', 'HITEST_STOP_FILE'): env.pop(key, None)
        with self.output.open('w') as handle:
            process = subprocess.Popen([str(self.project / 'hitest'), 'nvidia-load-unload', *args], env=env, cwd='/tmp', stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(process)
        return process

    def cleanup_processes(self):
        for process in self.processes:
            if process.poll() is None: os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def result(self, process, code, state, rounds):
        self.assertEqual(process.wait(timeout=10), code, self.output.read_text())
        run_dir = sorted((self.project / 'logs/nvidia-load-unload').iterdir())[-1]
        result = (run_dir / 'result.txt').read_text()
        self.assertIn(f'driver_state={state}\n', result)
        self.assertIn(f'completed_rounds={rounds}\n', result)
        self.assertIn(f'exit_code={code}\n', result)
        self.assertTrue((run_dir / 'sut_dmesg.txt').exists())
        return run_dir, (run_dir / 'run.log').read_text()

    def await_output(self, process, marker):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker in self.output.read_text(): return
            if process.poll() is not None: break
            time.sleep(0.005)
        self.fail(self.output.read_text())

    def test_two_rounds_order_gpu_queries_and_final_unloaded(self):
        run_dir, log = self.result(self.start(('2', '0')), 0, 'unloaded', 2)
        operations = [line for line in log.splitlines() if line.startswith('BEGIN ')]
        cycle = ['BEGIN load nvidia', 'BEGIN load nvidia_uvm', 'BEGIN load nvidia_modeset', 'BEGIN load nvidia_drm',
                 'BEGIN unload nvidia_drm', 'BEGIN unload nvidia_modeset', 'BEGIN unload nvidia_uvm', 'BEGIN unload nvidia']
        self.assertEqual(operations, cycle * 2)
        self.assertEqual(list(self.modules.iterdir()), [])
        self.assertIn('GPU-fixture', (run_dir / 'nvidia_smi_round_1.csv').read_text())

    def test_initial_stack_including_optional_peer_and_fs_unloaded_first(self):
        for module in ('nvidia', 'nvidia_uvm', 'nvidia_modeset', 'nvidia_drm', 'nvidia_peermem', 'nvidia_fs'): self.loaded(module)
        _, log = self.result(self.start(), 0, 'unloaded', 1)
        operations = [line for line in log.splitlines() if line.startswith('BEGIN ')]
        self.assertEqual(operations[:6], ['BEGIN unload ' + m for m in ('nvidia_fs', 'nvidia_peermem', 'nvidia_drm', 'nvidia_modeset', 'nvidia_uvm', 'nvidia')])
        self.assertIn('BEGIN load nvidia_peermem', operations)
        self.assertIn('BEGIN load nvidia_fs', operations)

    def test_compute_only_installation_without_display_modules(self):
        _, log = self.result(self.start(MOCK_HEADLESS='1'), 0, 'unloaded', 1)
        self.assertNotIn('BEGIN load nvidia_drm', log)
        self.assertNotIn('BEGIN load nvidia_modeset', log)

    def test_readonly_preflight_never_calls_smi_or_modprobe(self):
        self.loaded('nvidia')
        _, log = self.result(self.start(('--check',)), 0, 'unchanged', 0)
        self.assertNotIn('BEGIN ', log)
        self.assertTrue((self.modules / 'nvidia').exists())
        self.assertNotIn('rounds', [path.name for path in self.root.iterdir()])

    def test_busy_device_rejected_before_any_unload(self):
        self.loaded('nvidia')
        _, log = self.result(self.start(MOCK_BUSY='1'), 1, 'unchanged', 0)
        self.assertNotIn('BEGIN ', log)
        self.assertIn('1234', log)
        self.assertTrue((self.modules / 'nvidia').exists())

    def test_external_holder_rejected_before_any_unload(self):
        self.loaded('nvidia')
        (self.modules / 'nvidia/holders/third_party').touch()
        _, log = self.result(self.start(), 1, 'unchanged', 0)
        self.assertIn('third_party', log)
        self.assertNotIn('BEGIN ', log)

    def test_load_failure_stops_without_retrying_or_starting_next_round(self):
        _, log = self.result(self.start(('3', '0'), MOCK_FAIL='load:nvidia_uvm'), 7, 'unknown', 0)
        self.assertEqual(log.count('BEGIN load nvidia'), 2)  # core plus UVM
        self.assertNotIn('BEGIN unload ', log)
        self.assertNotIn('第 2/3 轮', log)

    def test_unload_failure_stops_without_force_or_retry(self):
        _, log = self.result(self.start(('3', '0'), MOCK_FAIL='unload:nvidia_drm'), 7, 'unknown', 0)
        self.assertEqual(log.count('BEGIN unload nvidia_drm'), 1)
        self.assertNotIn('BEGIN unload nvidia_modeset', log)
        self.assertNotIn('第 2/3 轮', log)

    def test_module_remaining_after_unload_is_failure(self):
        _, log = self.result(self.start(MOCK_KEEP='nvidia_drm'), 1, 'unknown', 0)
        self.assertIn('卸载后仍存在', log)

    def test_smi_failure_cleans_loaded_modules_and_preserves_failure(self):
        _, log = self.result(self.start(MOCK_SMI_FAIL='1'), 9, 'unloaded', 0)
        self.assertIn('BEGIN unload nvidia', log)
        self.assertEqual(list(self.modules.iterdir()), [])

    def test_empty_smi_output_is_not_success(self):
        self.result(self.start(MOCK_SMI_EMPTY='1'), 1, 'unloaded', 0)

    def test_changed_gpu_set_fails_and_cleans_second_round(self):
        self.result(self.start(('3', '0'), MOCK_CHANGE='1'), 1, 'unloaded', 1)
        self.assertEqual(list(self.modules.iterdir()), [])

    def test_no_nvidia_gpu_or_nouveau_is_rejected(self):
        (self.pci / '0000:01:00.0/vendor').write_text('0x1002\n')
        _, log = self.result(self.start(), 1, 'unchanged', 0)
        self.assertNotIn('BEGIN ', log)
        (self.pci / '0000:01:00.0/vendor').write_text('0x10de\n')
        self.loaded('nouveau')
        _, log = self.result(self.start(), 1, 'unchanged', 0)
        self.assertIn('nouveau', log)
        self.assertNotIn('BEGIN ', log)

    def test_interrupt_loading_waits_current_command_then_unloads_partial_stack(self):
        process = self.start(('3', '0'))
        self.await_output(process, 'BEGIN load nvidia')
        os.killpg(process.pid, signal.SIGINT)
        _, log = self.result(process, 130, 'unloaded', 0)
        self.assertIn('END load nvidia', log)
        self.assertIn('END unload nvidia', log)
        self.assertEqual(list(self.modules.iterdir()), [])

    def test_terminate_during_waiting_finishes_unload_without_next_round(self):
        process = self.start(('3', '999999999'))
        self.await_output(process, 'sleep 999999999')
        os.kill(process.pid, signal.SIGTERM)
        _, log = self.result(process, 143, 'unloaded', 1)
        self.assertNotIn('第 2/3 轮', log)
        self.assertEqual(list(self.modules.iterdir()), [])

    def test_invalid_arguments_never_touch_driver(self):
        for args in [('0',), ('-1',), ('1', '-1'), ('1', '01'), ('1', '0', 'extra')]:
            with self.subTest(args=args):
                env = dict(os.environ, HITEST_RUN_DIR=str(self.root))
                process = subprocess.run(['bash', str(self.project / 'scripts/nvidia-load-unload.sh'), *args], env=env, capture_output=True, text=True)
                self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
                self.assertNotIn('BEGIN ', process.stdout)

    def test_unsafe_lock_symlink_is_rejected_without_truncating_destination(self):
        target = self.root / 'keep.txt'
        target.write_text('keep this content')
        (self.root / 'test.lock').symlink_to(target)
        process = self.start()
        self.assertEqual(process.wait(timeout=10), 1, self.output.read_text())
        self.assertEqual(target.read_text(), 'keep this content')
        self.assertNotIn('BEGIN ', self.output.read_text())


if __name__ == '__main__': unittest.main()
