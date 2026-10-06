#!/usr/bin/env python3
"""在临时副本中模拟驱动与 sudo，不操作真实设备。"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class SafeStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hitest-signal-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project with spaces"
        (self.project / "scripts").mkdir(parents=True)
        shutil.copy2(PROJECT / "hitest", self.project / "hitest")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.make_mock("sudo", '#!/bin/bash\nif [[ "$1" == -v ]]; then exit 0; fi\n[[ "$1" != -n ]] || shift\nexec "$@"\n')
        self.make_mock("dmesg", '#!/bin/bash\necho mock-kernel-log\n')
        self.make_mock("sleep", '#!/usr/bin/env python3\nimport time\nprint("MOCK_SLEEP", flush=True)\ntime.sleep(0.01)\n')
        self.make_mock("hy-smi", '''#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
operation = sys.argv[1]
print("BEGIN " + operation, flush=True)
time.sleep(0.3)
if os.environ.get("MOCK_FAIL") == operation:
    print("FAIL " + operation, flush=True)
    sys.exit(7)
module = Path(os.environ["MOCK_MODULE"])
if operation == "--loaddriver":
    module.mkdir(exist_ok=True)
elif module.exists() and os.environ.get("MOCK_KEEP_MODULE") != "1":
    module.rmdir()
print("END " + operation, flush=True)
''')
        version = self.root / "version"
        version.write_text("mock-driver-version\n")
        source = (PROJECT / "scripts/load-unload.sh").read_text()
        source = source.replace("HY_SMI=/opt/hyhal/bin/hy-smi", f'HY_SMI="{self.bin / "hy-smi"}"')
        source = source.replace("/opt/hyhal/.info/version", f'"{version}"')
        self.module = self.root / "hycu"
        source = source.replace("/sys/module/hycu", str(self.module))
        (self.project / "scripts/load-unload.sh").write_text(source)
        self.output = self.root / "console.txt"

    def make_mock(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def start(self, fail="", rounds="3", direct=False, loaded=False, keep_module=False, seconds=None):
        if loaded:
            self.module.mkdir()
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", MOCK_FAIL=fail,
                   MOCK_MODULE=str(self.module), MOCK_KEEP_MODULE=str(int(keep_module)))
        for key in ("HITEST_RUN_DIR", "HITEST_LOG_ROOT", "HITEST_STOP_FILE"):
            env.pop(key, None)
        args = [str(self.project / "hitest"), "load-unload", rounds]
        if direct:
            args = ["bash", str(self.project / "scripts/load-unload.sh"), rounds]
        if seconds is not None:
            args.append(seconds)
        with self.output.open("w") as out:
            self.process = subprocess.Popen(args, cwd="/tmp", env=env, stdout=out,
                                            stderr=subprocess.STDOUT, start_new_session=True)
        self.addCleanup(self.cleanup_process)

    def cleanup_process(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()

    def await_output(self, marker):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if marker in self.output.read_text():
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.01)
        self.fail(f"Missing {marker}: {self.output.read_text()}")

    def check_result(self, code, state="unloaded", rounds=1):
        self.assertEqual(self.process.wait(timeout=10), code, self.output.read_text())
        run_dir, = (self.project / "logs/load-unload").iterdir()
        result = (run_dir / "result.txt").read_text()
        log = (run_dir / "run.log").read_text()
        self.assertIn(f"exit_code={code}\n", result)
        self.assertIn("finished_at=", result)
        self.assertIn(f"driver_state={state}\n", result)
        self.assertIn(f"completed_rounds={rounds}\n", result)
        self.assertIn("mock-kernel-log", (run_dir / "sut_dmesg.txt").read_text())
        self.assertIn("结束，退出码：", log)
        self.assertNotIn("第 2/3 轮", log)
        return log

    def stop_at(self, marker, sig=signal.SIGINT, group=True, repeat=False, fail="", direct=False):
        self.start(fail=fail, direct=direct)
        self.await_output(marker)
        send = (lambda: os.killpg(self.process.pid, sig)) if group else (lambda: os.kill(self.process.pid, sig))
        send()
        if repeat:
            time.sleep(0.04)
            send()
        log = self.check_result(7 if fail else 128 + sig, "unknown" if fail else "unloaded", 0 if fail else 1)
        if not fail:
            self.assertEqual(log.count("END --loaddriver"), 1)
            self.assertEqual(log.count("END --unloaddriver"), 1)

    def test_interrupt_during_load(self):
        self.stop_at("BEGIN --loaddriver")

    def test_interrupt_during_sleep(self):
        self.stop_at("sleep 100")

    def test_interrupt_during_unload(self):
        self.stop_at("BEGIN --unloaddriver")

    def test_repeated_interrupt(self):
        self.stop_at("BEGIN --loaddriver", repeat=True)

    def test_term_to_entry_only(self):
        self.stop_at("BEGIN --loaddriver", sig=signal.SIGTERM, group=False)

    def test_term_to_group(self):
        self.stop_at("BEGIN --loaddriver", sig=signal.SIGTERM)

    def test_direct_script_entry(self):
        self.stop_at("BEGIN --loaddriver", direct=True)

    def test_unload_failure_after_interrupt(self):
        self.stop_at("BEGIN --loaddriver", fail="--unloaddriver")

    def test_load_failure(self):
        self.start(fail="--loaddriver")
        log = self.check_result(7, "unknown", 0)
        self.assertNotIn("BEGIN --unloaddriver", log)

    def test_normal_completion(self):
        self.start(rounds="1")
        log = self.check_result(0)
        operations = [line for line in log.splitlines() if line.startswith("BEGIN ")]
        self.assertEqual(operations, ["BEGIN --loaddriver", "BEGIN --unloaddriver"])
        self.assertEqual(log.count("MOCK_SLEEP"), 100)

    def test_custom_rounds_and_sleep(self):
        self.start(rounds="2", seconds="3")
        log = self.check_result(0, rounds=2)
        self.assertEqual(log.count("BEGIN --loaddriver"), 2)
        self.assertEqual(log.count("BEGIN --unloaddriver"), 2)
        self.assertEqual(log.count("MOCK_SLEEP"), 6)
        run_dir, = (self.project / "logs/load-unload").iterdir()
        result = (run_dir / "result.txt").read_text()
        self.assertIn("total_rounds=2\n", result)
        self.assertIn("sleep_seconds=3\n", result)

    def test_zero_sleep_direct_entry(self):
        self.start(rounds="2", seconds="0", direct=True)
        log = self.check_result(0, rounds=2)
        self.assertEqual(log.count("BEGIN --loaddriver"), 2)
        self.assertNotIn("MOCK_SLEEP", log)

    def test_interrupt_custom_sleep(self):
        self.start(rounds="2", seconds="999999999")
        self.await_output("sleep 999999999")
        os.killpg(self.process.pid, signal.SIGINT)
        self.check_result(130)

    def test_invalid_parameters(self):
        cases = [("0",), ("",), ("-1",), ("1000000000",),
                 ("1", "-1"), ("1", "1.5"), ("1", "01"), ("1", ""),
                 ("1", "abc"), ("1", "1000000000"), ("1", "0", "extra")]
        for args in cases:
            with self.subTest(args=args):
                env = dict(os.environ, HITEST_RUN_DIR=str(self.root))
                proc = subprocess.run(["bash", str(self.project / "scripts/load-unload.sh"), *args],
                                      env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      timeout=5)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn("用法", proc.stdout)
                self.assertNotIn("BEGIN ", proc.stdout)

    def test_initially_loaded(self):
        self.start(rounds="1", loaded=True)
        log = self.check_result(0)
        operations = [line for line in log.splitlines() if line.startswith("BEGIN ")]
        self.assertEqual(operations, ["BEGIN --unloaddriver", "BEGIN --loaddriver", "BEGIN --unloaddriver"])
        run_dir, = (self.project / "logs/load-unload").iterdir()
        self.assertIn("initial_driver_state=loaded\n", (run_dir / "result.txt").read_text())
        self.assertTrue((run_dir / "dmesg_initial_unload.txt").exists())

    def test_interrupt_initial_unload(self):
        self.start(loaded=True)
        self.await_output("BEGIN --unloaddriver")
        os.killpg(self.process.pid, signal.SIGINT)
        log = self.check_result(130, rounds=0)
        self.assertIn("END --unloaddriver", log)
        self.assertNotIn("BEGIN --loaddriver", log)

    def test_initial_unload_failure(self):
        self.start(loaded=True, fail="--unloaddriver")
        log = self.check_result(7, "unknown", 0)
        self.assertNotIn("BEGIN --loaddriver", log)

    def test_initial_unload_leaves_module(self):
        self.start(loaded=True, keep_module=True)
        log = self.check_result(1, "unknown", 0)
        self.assertNotIn("BEGIN --loaddriver", log)


if __name__ == "__main__":
    unittest.main()
