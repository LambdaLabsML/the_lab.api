"""Run recurring Codex turns with a minimum start-to-start interval."""

import argparse
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_SESSION_ID_RE = re.compile(
    r"^session id:\s*"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*$"
)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _normalise_duration(value: str) -> int:
    match = _DURATION_RE.fullmatch(value)
    if match is None or int(match.group(1)) < 1:
        raise ValueError("duration must be a positive integer followed by s, m, h, or d")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def _build_turn_command(
    codex_bin: str,
    model: str | None,
    extra_args: list[str],
    thread_id: str | None,
    prompt: str,
) -> list[str]:
    command = [codex_bin, *extra_args, "exec"]
    if thread_id is not None:
        command.append("resume")
    command.append("--dangerously-bypass-approvals-and-sandbox")
    if model:
        command.extend(["--model", model])
    if thread_id is not None:
        command.append(thread_id)
    command.append(prompt)
    return command


def _thread_id_from_line(line: str) -> str | None:
    match = _SESSION_ID_RE.fullmatch(line.strip())
    return match.group(1) if match else None


def _run_schedule(
    interval_seconds: int,
    run_turn: Callable[[str | None], str],
    stop: threading.Event,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    thread_id = None
    while not stop.is_set():
        started = clock()
        thread_id = run_turn(thread_id)
        if stop.is_set():
            break
        elapsed = clock() - started
        if stop.wait(max(0, interval_seconds - elapsed)):
            break


_active_process: subprocess.Popen[str] | None = None
_stop = threading.Event()
_signal_received: int | None = None


def _terminate_process(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
    except OSError:
        pass


def _activate_process(process: subprocess.Popen[str]) -> None:
    global _active_process
    _active_process = process
    if _stop.is_set():
        _terminate_process(process)


def _run_turn(command: list[str], expected_thread_id: str | None) -> str:
    if _stop.is_set():
        raise InterruptedError("Codex loop was stopped before the turn started")
    process = subprocess.Popen(
        command,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    _activate_process(process)
    thread_id = None
    try:
        if process.stderr is None:
            raise RuntimeError("Codex loop could not open its error pipe")
        for line in process.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()
            thread_id = _thread_id_from_line(line) or thread_id
        return_code = process.wait()
    finally:
        _active_process = None
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)
    if thread_id is None:
        raise RuntimeError("Codex did not report a thread id")
    if expected_thread_id is not None and thread_id != expected_thread_id:
        raise RuntimeError("Codex resumed a different thread")
    return thread_id


def _handle_signal(signum: int, _frame) -> None:
    global _signal_received
    _signal_received = signum
    _stop.set()
    if _active_process is not None:
        _terminate_process(_active_process)


def main(argv: list[str] | None = None) -> int:
    global _signal_received
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--duration", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--model")
    parser.add_argument("codex_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    extra_args = args.codex_args[1:] if args.codex_args[:1] == ["--"] else args.codex_args
    try:
        interval_seconds = _normalise_duration(args.duration)
    except ValueError as error:
        parser.error(str(error))

    _stop.clear()
    _signal_received = None
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _handle_signal)

    print(f"Codex loop minimum interval: {args.duration}", file=sys.stderr)

    def run_turn(thread_id: str | None) -> str:
        command = _build_turn_command(
            args.codex_bin,
            args.model,
            extra_args,
            thread_id,
            args.prompt,
        )
        return _run_turn(command, thread_id)

    try:
        _run_schedule(interval_seconds, run_turn, _stop)
    except subprocess.CalledProcessError as error:
        if not _stop.is_set():
            print(f"Codex turn failed with status {error.returncode}", file=sys.stderr)
            return error.returncode
    except (OSError, RuntimeError) as error:
        if not _stop.is_set():
            print(f"Codex loop failed: {error}", file=sys.stderr)
            return 1
    return 128 + _signal_received if _signal_received is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
