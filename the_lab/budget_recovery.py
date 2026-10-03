"""Strict, opt-in recovery of the legacy ARC worker budget exit protocol."""
import hashlib
import json
import re

from .runner import _metrics_from_result


def validate_budget_completion(exp: dict, output: str) -> dict:
    meta = exp.get("meta") or {}
    if exp.get("status") != "failed":
        raise ValueError("only failed experiments can be recovered")
    if int(meta.get("slurm_restart_count") or 0) != 0:
        raise ValueError("restart generations require a separate provenance audit")
    workers = int(meta.get("PARALLEL_WORKERS") or 1)
    seconds = int(meta.get("RUN_WALL_TIME_LIMIT_S") or meta.get("MAX_WALL_SECONDS") or 0)
    matches = re.findall(r"\[parallel\] workers done, exit codes (\[[^\n]+\])", output)
    if len(matches) != 1 or workers < 1 or seconds < 1:
        raise ValueError("one complete worker exit summary and configured budget are required")
    codes = json.loads(matches[0])
    if (not isinstance(codes, list) or len(codes) != workers or 3 not in codes
            or any(type(code) is not int or code not in (0, 3) for code in codes)):
        raise ValueError("worker exit list is incomplete or contains genuine failures")
    observed = {(int(worker), int(budget)) for worker, budget in re.findall(
        r"\[w(\d+)\][^\n]*?\[agent\] WALL TIME LIMIT: (\d+)s", output)}
    if any((i, seconds) not in observed for i, code in enumerate(codes) if code == 3):
        raise ValueError("every code-3 worker must have matching watchdog evidence")
    result = None
    for line in reversed(output.splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("metrics"), dict):
            result = candidate
            break
    if not result or not result["metrics"]:
        raise ValueError("no final metrics block was found")
    metrics, _ = _metrics_from_result(result)
    return {"metrics": metrics, "worker_exit_codes": codes, "budget_seconds": seconds,
            "log_sha256": hashlib.sha256(output.encode()).hexdigest()}


def validate_context_budget_completion(exp: dict, output: str) -> dict:
    """Validate a partial completion where only strict-context caps exited.

    A context cap happens before an HTTP request.  Legacy workers propagated
    that expected per-game condition as exit code 1, so recovery is allowed
    only when every such exit has its exact logged ``ContextBudgetExceeded``
    evidence.  Other exit codes, missing evidence, or a missing final metrics
    record remain failures.
    """
    meta = exp.get("meta") or {}
    if exp.get("status") != "failed":
        raise ValueError("only failed experiments can be recovered")
    workers = int(meta.get("PARALLEL_WORKERS") or 1)
    seconds = int(meta.get("RUN_WALL_TIME_LIMIT_S") or meta.get("MAX_WALL_SECONDS") or 0)
    matches = re.findall(r"\[parallel\] workers done, exit codes (\[[^\n]+\])", output)
    if len(matches) != 1 or workers < 1 or seconds < 1:
        raise ValueError("one complete worker exit summary and configured budget are required")
    codes = json.loads(matches[0])
    if (not isinstance(codes, list) or len(codes) != workers
            or 1 not in codes or any(type(code) is not int or code not in (0, 1, 3) for code in codes)):
        raise ValueError("worker exit list contains an unrecoverable failure")
    capped_workers = {
        int(worker) for worker in re.findall(
            r"\[w(\d+)\].*agent\.context_budget\.ContextBudgetExceeded: .*No request sent\.",
            output,
        )
    }
    failed_workers = {index for index, code in enumerate(codes) if code == 1}
    if capped_workers != failed_workers:
        raise ValueError("every code-1 worker must be explained by one strict-context cap")
    observed_budget = {(int(worker), int(budget)) for worker, budget in re.findall(
        r"\[w(\d+)\][^\n]*?\[agent\] WALL TIME LIMIT: (\d+)s", output,
    )}
    if any((index, seconds) not in observed_budget for index, code in enumerate(codes) if code == 3):
        raise ValueError("every code-3 worker must have matching watchdog evidence")
    result = None
    for line in reversed(output.splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("metrics"), dict):
            result = candidate
            break
    if not result or not result["metrics"]:
        raise ValueError("no final metrics block was found")
    metrics, _ = _metrics_from_result(result)
    return {
        "metrics": metrics,
        "worker_exit_codes": codes,
        "context_capped_workers": sorted(capped_workers),
        "budget_seconds": seconds,
        "restart_generation": int(meta.get("slurm_restart_count") or 0),
        "log_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }
