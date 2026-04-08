#!/usr/bin/env python3
"""Evaluate fast math kernels: correctness and throughput.

Tests each kernel against the reference implementation over a dense input
range, then benchmarks throughput. Outputs Lab-compatible JSON to stdout.

Usage:
    python benchmark/eval_harness.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

# Ensure the project root is on sys.path so 'benchmark' is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark import reference as ref
from benchmark import kernels as k

# ---------------------------------------------------------------------------
# Test ranges for each function
# ---------------------------------------------------------------------------

TESTS = {
    "sin":     {"func": k.fast_sin,     "ref": ref.ref_sin,     "args": "single", "range": (-6.3, 6.3, 2000)},
    "cos":     {"func": k.fast_cos,     "ref": ref.ref_cos,     "args": "single", "range": (-6.3, 6.3, 2000)},
    "exp":     {"func": k.fast_exp,     "ref": ref.ref_exp,     "args": "single", "range": (-10, 10, 2000)},
    "log":     {"func": k.fast_log,     "ref": ref.ref_log,     "args": "single", "range": (0.001, 1000, 2000)},
    "sqrt":    {"func": k.fast_sqrt,    "ref": ref.ref_sqrt,    "args": "single", "range": (0, 1e6, 2000)},
    "atan2":   {"func": k.fast_atan2,   "ref": ref.ref_atan2,   "args": "double", "range": (-10, 10, 500)},
    "sigmoid": {"func": k.fast_sigmoid, "ref": ref.ref_sigmoid, "args": "single", "range": (-20, 20, 2000)},
    "tanh":    {"func": k.fast_tanh,    "ref": ref.ref_tanh,    "args": "single", "range": (-10, 10, 2000)},
}


def linspace(lo: float, hi: float, n: int) -> list[float]:
    if n <= 1:
        return [lo]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def evaluate_single(name: str, spec: dict) -> dict:
    """Evaluate a single kernel: accuracy + throughput."""
    func = spec["func"]
    ref_func = spec["ref"]
    lo, hi, n = spec["range"]

    if spec["args"] == "single":
        inputs = [(x,) for x in linspace(lo, hi, n)]
    else:
        # double: grid of (y, x) pairs
        vals = linspace(lo, hi, int(math.sqrt(n)))
        inputs = [(y, x) for y in vals for x in vals if not (x == 0 and y == 0)]

    # --- Accuracy ---
    max_abs_err = 0.0
    max_rel_err = 0.0
    errors = 0
    for args in inputs:
        try:
            got = func(*args)
            expected = ref_func(*args)
        except Exception:
            errors += 1
            continue

        if math.isnan(expected) or math.isinf(expected):
            continue

        abs_err = abs(got - expected)
        rel_err = abs_err / max(abs(expected), 1e-15)
        max_abs_err = max(max_abs_err, abs_err)
        max_rel_err = max(max_rel_err, rel_err)

    accuracy = max(0.0, 1.0 - max_rel_err)

    # --- Throughput ---
    # Warm up
    for args in inputs[:100]:
        try:
            func(*args)
        except Exception:
            pass

    # Timed run (multiple passes for stability)
    n_passes = 5
    total_calls = len(inputs) * n_passes
    t0 = time.perf_counter()
    for _ in range(n_passes):
        for args in inputs:
            func(*args)
    elapsed = time.perf_counter() - t0
    ns_per_call = (elapsed / total_calls) * 1e9

    return {
        "accuracy": round(accuracy, 8),
        "max_abs_error": max_abs_err,
        "max_rel_error": max_rel_err,
        "ns_per_call": round(ns_per_call, 1),
        "errors": errors,
    }


def compute_score(results: dict[str, dict]) -> float:
    """Composite score: geometric mean of per-kernel scores.

    Per-kernel score = accuracy * (1000 / ns_per_call), capped at accuracy >= 0.99.
    This rewards both accuracy AND speed, with a hard accuracy floor.
    """
    scores = []
    for name, r in results.items():
        acc = r["accuracy"]
        if acc < 0.99:
            # Below accuracy floor: heavy penalty
            scores.append(acc * 0.1)
        else:
            # Score = speed (higher = faster), scaled so 1000ns = 1.0
            scores.append(acc * (1000.0 / max(r["ns_per_call"], 1.0)))
    if not scores:
        return 0.0
    # Geometric mean
    product = 1.0
    for s in scores:
        product *= s
    return round(product ** (1.0 / len(scores)), 6)


def main():
    results = {}
    for name, spec in TESTS.items():
        results[name] = evaluate_single(name, spec)

    score = compute_score(results)

    output = {
        "metrics": {
            "score": score,
            **{f"{name}_accuracy": r["accuracy"] for name, r in results.items()},
            **{f"{name}_ns": r["ns_per_call"] for name, r in results.items()},
        },
        "meta": {
            "kernels_tested": len(results),
            "per_kernel": results,
        },
    }

    # Print per-kernel summary to stderr for human readability
    print("Kernel Results:", file=sys.stderr)
    print(f"{'kernel':<10} {'accuracy':>10} {'ns/call':>10} {'errors':>7}", file=sys.stderr)
    print("-" * 40, file=sys.stderr)
    for name, r in results.items():
        print(f"{name:<10} {r['accuracy']:>10.6f} {r['ns_per_call']:>10.1f} {r['errors']:>7}", file=sys.stderr)
    print(f"\nComposite score: {score}", file=sys.stderr)

    # Lab-compatible JSON on stdout
    print(json.dumps(output))


if __name__ == "__main__":
    main()
