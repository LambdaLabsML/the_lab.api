import importlib
import inspect
import signal
import sys
import unittest
from io import StringIO
from unittest.mock import patch

from the_lab import agent_cli
from the_lab.agent_cli import _build_launch_command


def _loop_module():
    try:
        return importlib.import_module("the_lab.codex_loop")
    except ModuleNotFoundError as error:
        raise AssertionError("Codex loop runner is missing") from error


class CodexLoopTests(unittest.TestCase):
    def test_claude_keeps_native_loop_prompt(self):
        command = _build_launch_command(
            "claude",
            "claude",
            "/loop 5m keep working",
            None,
            False,
        )

        self.assertEqual(
            command,
            ["claude", "--dangerously-skip-permissions", "/loop 5m keep working"],
        )

    def test_codex_loop_launches_scheduler_without_slash_command(self):
        parameters = inspect.signature(_build_launch_command).parameters
        if "loop_duration" not in parameters:
            self.fail("Codex launch command has no loop scheduler support")

        command = _build_launch_command(
            "codex",
            "codex",
            "keep working",
            "gpt-test",
            False,
            extra_agent_args=["--search"],
            loop_duration="5m",
        )

        self.assertEqual(command[:3], [sys.executable, "-m", "the_lab.codex_loop"])
        self.assertIn("5m", command)
        self.assertIn("keep working", command)
        self.assertNotIn("/loop", " ".join(command))

    def test_only_codex_loop_disables_interactive_child_input(self):
        if not hasattr(agent_cli, "_uses_interactive_child"):
            self.fail("agent CLI cannot distinguish the headless Codex loop")

        self.assertFalse(agent_cli._uses_interactive_child("codex", True))
        self.assertTrue(agent_cli._uses_interactive_child("codex", False))
        self.assertTrue(agent_cli._uses_interactive_child("claude", True))

    def test_advertised_durations_map_to_seconds(self):
        normalise = _loop_module()._normalise_duration

        self.assertEqual(normalise("30s"), 30)
        self.assertEqual(normalise("61s"), 61)
        self.assertEqual(normalise("5m"), 300)
        self.assertEqual(normalise("2h"), 7_200)
        self.assertEqual(normalise("1d"), 86_400)

    def test_turn_runs_immediately_then_waits_remaining_interval(self):
        module = _loop_module()
        times = iter((100.0, 220.0, 400.0))
        events = []
        turns = []

        class Stop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, delay):
                events.append(("wait", delay))
                return False

        stop = Stop()

        def run_turn(thread_id):
            turns.append(thread_id)
            events.append(("run", thread_id))
            if len(turns) == 2:
                stop.stopped = True
            return "thread-123"

        module._run_schedule(300, run_turn, stop, lambda: next(times))

        self.assertEqual(
            events,
            [("run", None), ("wait", 180), ("run", "thread-123")],
        )

    def test_turn_commands_resume_the_exact_thread_and_repeat_the_prompt(self):
        build = _loop_module()._build_turn_command
        prompt = "run the next experiment"

        initial = build("codex", "gpt-test", ["--search"], None, prompt)
        resumed = build("codex", "gpt-test", ["--search"], "thread-123", prompt)

        self.assertEqual(
            initial,
            [
                "codex",
                "--search",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                "gpt-test",
                prompt,
            ],
        )
        self.assertEqual(
            resumed,
            [
                "codex",
                "--search",
                "exec",
                "resume",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                "gpt-test",
                "thread-123",
                prompt,
            ],
        )

    def test_long_turn_runs_once_immediately_without_catch_up_queue(self):
        module = _loop_module()
        times = iter((100.0, 800.0, 800.0, 810.0))
        waits = []
        turns = []

        class Stop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, delay):
                waits.append(delay)
                return len(waits) == 2

        stop = Stop()

        def run_turn(thread_id):
            turns.append(thread_id)
            return "thread-123"

        module._run_schedule(300, run_turn, stop, lambda: next(times))

        self.assertEqual(waits, [0, 290])
        self.assertEqual(turns, [None, "thread-123"])

    def test_stop_during_wait_prevents_another_turn(self):
        module = _loop_module()
        turns = []
        waits = []

        class Stop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, delay):
                waits.append(delay)
                self.stopped = True
                return False

        stop = Stop()

        module._run_schedule(
            300,
            lambda thread_id: turns.append(thread_id) or "thread-123",
            stop,
            iter((100.0, 101.0)).__next__,
        )

        self.assertEqual(turns, [None])
        self.assertEqual(waits, [299])

    def test_pending_signal_is_replayed_to_new_child(self):
        module = _loop_module()
        if not hasattr(module, "_activate_process"):
            self.fail("Codex loop cannot replay a signal received during spawn")

        class Process:
            def __init__(self):
                self.terminated = 0

            def poll(self):
                return None

            def terminate(self):
                self.terminated += 1

        process = Process()
        module._stop.set()
        module._signal_received = signal.SIGINT
        self.addCleanup(module._stop.clear)
        self.addCleanup(setattr, module, "_signal_received", None)
        self.addCleanup(setattr, module, "_active_process", None)

        module._activate_process(process)

        self.assertEqual(process.terminated, 1)

    def test_ctrl_c_terminates_active_child(self):
        module = _loop_module()

        class Process:
            terminated = 0

            def poll(self):
                return None

            def terminate(self):
                self.terminated += 1

        process = Process()
        module._active_process = process
        module._stop.clear()
        self.addCleanup(module._stop.clear)
        self.addCleanup(setattr, module, "_signal_received", None)
        self.addCleanup(setattr, module, "_active_process", None)

        module._handle_signal(signal.SIGINT, None)

        self.assertTrue(module._stop.is_set())
        self.assertEqual(process.terminated, 1)

    def test_run_turn_forwards_native_output_and_reads_session_id(self):
        module = _loop_module()
        lines = [
            "OpenAI Codex v0.149.0\n",
            "session id: 01a02689-b39b-7cd1-ac1d-5e32de2ab114\n",
            "codex\n",
        ]

        class Process:
            stderr = iter(lines)

            def poll(self):
                return None

            def wait(self):
                return 0

        output = StringIO()
        with (
            patch.object(module.subprocess, "Popen", return_value=Process()) as popen,
            patch.object(sys, "stderr", output),
        ):
            thread_id = module._run_turn(["codex"], None)

        self.assertEqual(thread_id, "01a02689-b39b-7cd1-ac1d-5e32de2ab114")
        self.assertEqual(output.getvalue(), "".join(lines))
        self.assertIs(popen.call_args.kwargs["stderr"], module.subprocess.PIPE)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("stdin", popen.call_args.kwargs)
        self.assertNotIn("stdout", popen.call_args.kwargs)

    def test_thread_id_is_read_from_native_codex_header(self):
        extract = _loop_module()._thread_id_from_line

        self.assertEqual(
            extract("session id: 01a02689-b39b-7cd1-ac1d-5e32de2ab114"),
            "01a02689-b39b-7cd1-ac1d-5e32de2ab114",
        )
        self.assertIsNone(extract("session id:"))
        self.assertIsNone(extract("session id: ----"))
        self.assertIsNone(extract("session id: deadbeef"))
        self.assertIsNone(extract("session id: a-b-c"))
        self.assertIsNone(extract("not a session header"))


if __name__ == "__main__":
    unittest.main()
