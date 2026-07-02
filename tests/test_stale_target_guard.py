import unittest

import lldbAiHelper_MCP_bridge as bridge_mod


class FakeLLDB:
    eStateInvalid = 0
    eStateUnloaded = 1
    eStateConnected = 2
    eStateAttaching = 3
    eStateLaunching = 4
    eStateStopped = 5
    eStateRunning = 6
    eStateStepping = 7
    eStateCrashed = 8
    eStateDetached = 9
    eStateExited = 10
    eStateSuspended = 11


class FakeError:
    def __init__(self, success=True, message=""):
        self._success = success
        self._message = message

    def Success(self):
        return self._success

    def __str__(self):
        return self._message or ("success" if self._success else "error")


class FakeFileSpec:
    def __init__(self, filename="unknown"):
        self.filename = filename

    def IsValid(self):
        return True

    def GetFilename(self):
        return self.filename


class FakePlatform:
    def __init__(self, name="remote-android"):
        self.name = name

    def IsValid(self):
        return True

    def GetName(self):
        return self.name


class FakeProcessInfo:
    def __init__(self, name=""):
        self.name = name

    def GetName(self):
        return self.name


class FakeProcess:
    def __init__(self, pid=1234, state=FakeLLDB.eStateRunning, stop_success=True, name=""):
        self.pid = pid
        self.state = state
        self.stop_success = stop_success
        self.name = name
        self.stop_calls = 0

    def IsValid(self):
        return True

    def GetState(self):
        return self.state

    def GetProcessID(self):
        return self.pid

    def GetNumThreads(self):
        return 7

    def GetProcessInfo(self):
        return FakeProcessInfo(self.name)

    def Stop(self):
        self.stop_calls += 1
        if self.stop_success:
            self.state = FakeLLDB.eStateStopped
            return FakeError(True)
        return FakeError(False, "error: Halt timed out. State = running")


class FakeTarget:
    def __init__(self, process, filename="unknown", platform="remote-android"):
        self.process = process
        self.filename = filename
        self.platform = platform

    def IsValid(self):
        return True

    def GetProcess(self):
        return self.process

    def GetExecutable(self):
        return FakeFileSpec(self.filename)

    def GetPlatform(self):
        return FakePlatform(self.platform)


class FakeInterpreter:
    def __init__(self):
        self.commands = []

    def HandleCommand(self, command, result):
        self.commands.append(command)


class FakeDebugger:
    def __init__(self, target):
        self.target = target
        self.interpreter = FakeInterpreter()
        self.async_values = []

    def GetSelectedTarget(self):
        return self.target

    def SetAsync(self, value):
        self.async_values.append(value)

    def GetAsync(self):
        return False

    def GetCommandInterpreter(self):
        return self.interpreter


class StaleTargetGuardTests(unittest.TestCase):
    def setUp(self):
        bridge_mod.lldb = FakeLLDB
        bridge_mod.LLDBBridge._STATE_MAP = None
        bridge_mod.LLDBBridge._STOP_REASON_MAP = None

    def make_bridge(self, pid=111, state=FakeLLDB.eStateRunning, stop_success=True, name=""):
        process = FakeProcess(pid=pid, state=state, stop_success=stop_success, name=name)
        target = FakeTarget(process)
        debugger = FakeDebugger(target)
        return bridge_mod.LLDBBridge(debugger), process, debugger

    def test_stop_returns_stale_target_before_calling_stop_when_adb_pid_differs(self):
        bridge, process, _debugger = self.make_bridge(pid=111)
        bridge._get_adb_pid_for_package = lambda package_name, adb_serial="", timeout=2.0: {
            "checked": True,
            "package_name": package_name,
            "pid": 222,
            "pids": [222],
        }

        result = bridge._cmd_stop_process(package_name="com.example.app")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "stale_target")
        self.assertEqual(result["lldb_pid"], 111)
        self.assertEqual(result["adb_current_pid"], 222)
        self.assertEqual(result["state"], "running")
        self.assertEqual(process.stop_calls, 0)

    def test_stop_returns_process_ended_before_calling_stop_for_ended_lldb_state(self):
        bridge, process, _debugger = self.make_bridge(pid=111, state=FakeLLDB.eStateExited)

        result = bridge._cmd_stop_process()

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "process_ended")
        self.assertEqual(result["state"], "exited")
        self.assertEqual(process.stop_calls, 0)

    def test_stop_returns_process_ended_when_adb_pidof_finds_no_process(self):
        bridge, process, _debugger = self.make_bridge(pid=111)
        bridge._get_adb_pid_for_package = lambda package_name, adb_serial="", timeout=2.0: {
            "checked": True,
            "package_name": package_name,
            "pid": None,
            "pids": [],
        }

        result = bridge._cmd_stop_process(package_name="com.example.app")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "process_ended")
        self.assertIsNone(result["adb_current_pid"])
        self.assertEqual(process.stop_calls, 0)

    def test_stop_returns_process_ended_when_android_lldb_pid_is_gone_without_package(self):
        bridge, process, _debugger = self.make_bridge(pid=111)
        bridge._get_adb_process_for_pid = lambda pid, adb_serial="", timeout=2.0: {
            "checked": True,
            "pid": pid,
            "pid_exists": False,
        }

        result = bridge._cmd_stop_process()

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "process_ended")
        self.assertTrue(result["diagnostics"]["adb_lldb_pid_checked"])
        self.assertFalse(result["diagnostics"]["adb_lldb_pid_alive"])
        self.assertEqual(process.stop_calls, 0)

    def test_stop_calls_lldb_stop_when_adb_pid_matches(self):
        bridge, process, debugger = self.make_bridge(pid=111)
        bridge._get_adb_pid_for_package = lambda package_name, adb_serial="", timeout=2.0: {
            "checked": True,
            "package_name": package_name,
            "pid": 111,
            "pids": [111],
        }

        result = bridge._cmd_stop_process(package_name="com.example.app")

        self.assertTrue(result["success"])
        self.assertEqual(process.stop_calls, 1)
        self.assertEqual(debugger.async_values[-1], False)

    def test_process_interrupt_command_passes_package_args_to_safe_stop_path(self):
        bridge, _process, debugger = self.make_bridge(pid=111)
        captured = {}

        def fake_stop(package_name="", adb_serial=""):
            captured["package_name"] = package_name
            captured["adb_serial"] = adb_serial
            return {
                "success": False,
                "reason": "redirected",
            }

        bridge._cmd_stop_process = fake_stop

        result = bridge._cmd_execute(
            "process interrupt",
            package_name="com.example.app",
            adb_serial="device-1",
        )

        self.assertEqual(result["reason"], "redirected")
        self.assertEqual(captured["package_name"], "com.example.app")
        self.assertEqual(captured["adb_serial"], "device-1")
        self.assertEqual(debugger.interpreter.commands, [])

    def test_interrupt_aliases_are_detected(self):
        expected = {
            "interrupt": "interrupt",
            "process interrupt": "process interrupt",
            "halt": "halt",
            "process halt": "process halt",
        }

        for command, matched in expected.items():
            with self.subTest(command=command):
                self.assertEqual(bridge_mod.LLDBBridge._detect_interrupt_command(command), matched)

    def test_execute_batch_rejects_interrupt_command(self):
        bridge, _process, debugger = self.make_bridge(pid=111)

        result = bridge._cmd_execute_batch(["bt", "process interrupt"])

        self.assertIn("拒绝执行中断命令", result)
        self.assertEqual(debugger.interpreter.commands, [])

    def test_process_interrupt_command_uses_safe_stop_path_without_args(self):
        bridge, _process, debugger = self.make_bridge(pid=111)
        bridge._cmd_stop_process = lambda package_name="", adb_serial="": {
            "success": False,
            "reason": "redirected",
        }

        result = bridge._cmd_execute("process interrupt")

        self.assertEqual(result["reason"], "redirected")
        self.assertEqual(debugger.interpreter.commands, [])


if __name__ == "__main__":
    unittest.main()
