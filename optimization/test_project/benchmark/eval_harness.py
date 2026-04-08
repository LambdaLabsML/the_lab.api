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
from pathlib import Path

# Ensure the project root is on sys.path so 'benchmark' is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark import reference as ref
from benchmark import kernels as k

# ---------------------------------------------------------------------------
# Test ranges for each function
# ---------------------------------------------------------------------------

# Dense test grids — enough points for thorough accuracy testing AND
# naturally slow evaluation (~30-90s total depending on kernel speed).
# 2M points × 3 passes × 7 kernels ≈ 60-120s with naive implementations.
TESTS = {
    "sin":     {"func": k.fast_sin,     "ref": ref.ref_sin,     "args": "single", "range": (-6.3, 6.3, 2000000)},
    "cos":     {"func": k.fast_cos,     "ref": ref.ref_cos,     "args": "single", "range": (-6.3, 6.3, 2000000)},
    "exp":     {"func": k.fast_exp,     "ref": ref.ref_exp,     "args": "single", "range": (-10, 10, 2000000)},
    "log":     {"func": k.fast_log,     "ref": ref.ref_log,     "args": "single", "range": (0.001, 1000, 2000000)},
    "sqrt":    {"func": k.fast_sqrt,    "ref": ref.ref_sqrt,    "args": "single", "range": (0, 1e6, 2000000)},
    "sigmoid": {"func": k.fast_sigmoid, "ref": ref.ref_sigmoid, "args": "single", "range": (-20, 20, 2000000)},
    "tanh":    {"func": k.fast_tanh,    "ref": ref.ref_tanh,    "args": "single", "range": (-10, 10, 2000000)},
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

    # Timed run (multiple passes for stable throughput measurement)
    n_passes = 3
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


def check_memory_budget() -> dict:
    """Check that TABLES fit within MEMORY_BUDGET."""
    total_floats = sum(len(t) for t in k.TABLES.values())
    total_bytes = total_floats * 8
    budget = k.MEMORY_BUDGET
    return {
        "total_bytes": total_bytes,
        "budget_bytes": budget,
        "within_budget": total_bytes <= budget,
        "utilization": round(total_bytes / budget, 3) if budget > 0 else 0,
        "tables": {name: len(t) * 8 for name, t in k.TABLES.items()},
    }


def compute_score(results: dict[str, dict], memory: dict) -> float:
    """Composite score: geometric mean of per-kernel scores.

    Per-kernel score:
      - accuracy < 0.9999 (99.99%): score = accuracy * 0.1 (heavy penalty)
      - accuracy >= 0.9999: score = accuracy * (1000 / ns_per_call)

    Memory penalty: if over budget, multiply total by 0.1.
    """
    scores = []
    for name, r in results.items():
        acc = r["accuracy"]
        if acc < 0.9999:
            # Below 99.99% accuracy floor
            scores.append(max(acc, 0.01) * 0.1)
        else:
            # Score = speed, scaled so 1000ns = 1.0
            scores.append(acc * (1000.0 / max(r["ns_per_call"], 1.0)))
    if not scores:
        return 0.0
    # Geometric mean
    product = 1.0
    for s in scores:
        product *= max(s, 1e-6)
    score = product ** (1.0 / len(scores))
    # Memory penalty
    if not memory.get("within_budget", True):
        score *= 0.1
    return round(score, 6)


def main():
    # Check memory budget first
    memory = check_memory_budget()

    results = {}
    for name, spec in TESTS.items():
        results[name] = evaluate_single(name, spec)

    score = compute_score(results, memory)

    output = {
        "metrics": {
            "score": score,
            "memory_used_bytes": memory["total_bytes"],
            "memory_budget_bytes": memory["budget_bytes"],
            "memory_within_budget": memory["within_budget"],
            **{f"{name}_accuracy": r["accuracy"] for name, r in results.items()},
            **{f"{name}_ns": r["ns_per_call"] for name, r in results.items()},
        },
        "meta": {
            "kernels_tested": len(results),
            "memory": memory,
            "per_kernel": results,
        },
    }

    # Print per-kernel summary to stderr for human readability
    print("Kernel Results:", file=sys.stderr)
    print(f"{'kernel':<10} {'accuracy':>10} {'ns/call':>10} {'errors':>7}", file=sys.stderr)
    print("-" * 40, file=sys.stderr)
    for name, r in results.items():
        print(f"{name:<10} {r['accuracy']:>10.6f} {r['ns_per_call']:>10.1f} {r['errors']:>7}", file=sys.stderr)
    print(f"\nMemory: {memory['total_bytes']}/{memory['budget_bytes']} bytes "
          f"({'OK' if memory['within_budget'] else 'OVER BUDGET!'})", file=sys.stderr)
    print(f"Composite score: {score}", file=sys.stderr)

    # Lab-compatible JSON on stdout
    print(json.dumps(output))


if __name__ == "__main__":
    main()
