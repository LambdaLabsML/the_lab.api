#!/usr/bin/env python3
"""Evaluate an autoresearch candidate by running train.py and parsing val_bpb.

The active optimization target is karpathy/autoresearch. Candidate branches
modify train.py, then this script runs the fixed training entry point and emits
Lab-compatible JSON metrics. The primary metric is val_bpb/loss, lower is
better.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


SUMMARY_PATTERNS = {
    "val_bpb": re.compile(r"^val_bpb:\s*([0-9.]+)", re.MULTILINE),
    "training_seconds": re.compile(r"^training_seconds:\s*([0-9.]+)", re.MULTILINE),
    "total_seconds": re.compile(r"^total_seconds:\s*([0-9.]+)", re.MULTILINE),
    "peak_vram_mb": re.compile(r"^peak_vram_mb:\s*([0-9.]+)", re.MULTILINE),
    "mfu_percent": re.compile(r"^mfu_percent:\s*([0-9.]+)", re.MULTILINE),
    "total_tokens_M": re.compile(r"^total_tokens_M:\s*([0-9.]+)", re.MULTILINE),
    "num_steps": re.compile(r"^num_steps:\s*([0-9]+)", re.MULTILINE),
    "num_params_M": re.compile(r"^num_params_M:\s*([0-9.]+)", re.MULTILINE),
    "depth": re.compile(r"^depth:\s*([0-9]+)", re.MULTILINE),
}


def parse_summary(text: str) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    for key, pattern in SUMMARY_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(1)
        if key in {"num_steps", "depth"}:
            metrics[key] = int(value)
        else:
            metrics[key] = float(value)
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=900, help="Kill train.py after this many seconds")
    parser.add_argument("--log", default="run.log", help="Path for captured train.py output")
    parser.add_argument(
        "--command",
        nargs=argparse.REMAINDER,
        default=["uv", "run", "train.py"],
        help="Training command; keep this as the final option when used",
    )
    args = parser.parse_args()

    repo = Path.cwd()
    train_py = repo / "train.py"
    if not train_py.exists():
        raise SystemExit(f"train.py not found in {repo}")

    start = time.time()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    log_path = Path(args.log)

    command = args.command or ["uv", "run", "train.py"]

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(command)}\n")
        log.flush()
        proc = subprocess.run(
            command,
            cwd=repo,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=args.timeout,
        )

    text = log_path.read_text(encoding="utf-8", errors="replace")
    parsed = parse_summary(text)
    elapsed = time.time() - start

    if proc.returncode != 0 or "val_bpb" not in parsed:
        tail = "\n".join(text.splitlines()[-80:])
        result = {
            "metrics": {
                "status_code": proc.returncode,
                "loss": None,
                "val_bpb": None,
                "runtime_s": round(elapsed, 1),
            },
            "meta": {
                "target": "karpathy/autoresearch",
                "optimized_file": "train.py",
                "metric_direction": {"loss": "minimize", "val_bpb": "minimize"},
                "log": str(log_path),
                "error_tail": tail,
            },
        }
        print(json.dumps(result))
        return 1

    val_bpb = float(parsed["val_bpb"])
    metrics = {
        "loss": val_bpb,
        "val_bpb": val_bpb,
        "status_code": proc.returncode,
        "runtime_s": round(elapsed, 1),
        **parsed,
    }
    if "peak_vram_mb" in metrics:
        metrics["peak_vram_gb"] = round(float(metrics["peak_vram_mb"]) / 1024, 3)

    result = {
        "metrics": metrics,
        "meta": {
            "target": "karpathy/autoresearch",
            "optimized_file": "train.py",
            "primary_metric": "val_bpb",
            "metric_direction": {"loss": "minimize", "val_bpb": "minimize"},
            "command": command,
            "log": str(log_path),
        },
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired as exc:
        result = {
            "metrics": {
                "loss": None,
                "val_bpb": None,
                "status_code": 124,
                "runtime_s": exc.timeout,
            },
            "meta": {
                "target": "karpathy/autoresearch",
                "optimized_file": "train.py",
                "primary_metric": "val_bpb",
                "metric_direction": {"loss": "minimize", "val_bpb": "minimize"},
                "error": f"training timed out after {exc.timeout}s",
            },
        }
        print(json.dumps(result))
        raise SystemExit(124)
