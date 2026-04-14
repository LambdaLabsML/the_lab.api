#!/usr/bin/env python3
"""Generate pre-seeded Lab fixtures for API comprehension tests.

Each fixture is a self-contained git repo with .the_lab/ data and a
math-kernel optimization project (benchmark/ directory).

Usage:
    python optimization/tests/seed_fixture.py            # regenerate all
    python optimization/tests/seed_fixture.py t1          # regenerate just T1
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

TESTS_DIR = Path(__file__).parent

_BASE_TIME = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)


def _ts(offset_hours: float = 0) -> str:
    """ISO timestamp offset from base time."""
    return (_BASE_TIME + timedelta(hours=offset_hours)).isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# Shared project files — the math kernel optimization problem
# ═══════════════════════════════════════════════════════════════════════════

BENCHMARK_INIT = ""

BENCHMARK_REFERENCE = '''\
"""Reference implementations — correct answers from math stdlib."""
import math


def ref_sin(x: float) -> float:
    return math.sin(x)

def ref_cos(x: float) -> float:
    return math.cos(x)

def ref_exp(x: float) -> float:
    return math.exp(x)

def ref_log(x: float) -> float:
    return math.log(x) if x > 0 else float("-inf")

def ref_sqrt(x: float) -> float:
    return math.sqrt(x) if x >= 0 else float("nan")

def ref_sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)

def ref_tanh(x: float) -> float:
    return math.tanh(x)
'''

BENCHMARK_EVAL_HARNESS = '''\
#!/usr/bin/env python3
"""Evaluate fast math kernels: correctness and throughput.

Tests each kernel against the reference implementation, then benchmarks
throughput. Outputs Lab-compatible JSON to stdout.

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

# ── Test ranges ──────────────────────────────────────────────────────────
# 50k points x 1 pass x 7 kernels => completes in ~2-5s
TESTS = {
    "sin":     {"func": k.fast_sin,     "ref": ref.ref_sin,     "args": "single", "range": (-6.3, 6.3, 50000)},
    "cos":     {"func": k.fast_cos,     "ref": ref.ref_cos,     "args": "single", "range": (-6.3, 6.3, 50000)},
    "exp":     {"func": k.fast_exp,     "ref": ref.ref_exp,     "args": "single", "range": (-10, 10, 50000)},
    "log":     {"func": k.fast_log,     "ref": ref.ref_log,     "args": "single", "range": (0.001, 1000, 50000)},
    "sqrt":    {"func": k.fast_sqrt,    "ref": ref.ref_sqrt,    "args": "single", "range": (0, 1e6, 50000)},
    "sigmoid": {"func": k.fast_sigmoid, "ref": ref.ref_sigmoid, "args": "single", "range": (-20, 20, 50000)},
    "tanh":    {"func": k.fast_tanh,    "ref": ref.ref_tanh,    "args": "single", "range": (-10, 10, 50000)},
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
    for args in inputs[:100]:
        try:
            func(*args)
        except Exception:
            pass

    n_passes = 1
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
            scores.append(max(acc, 0.01) * 0.1)
        else:
            scores.append(acc * (1000.0 / max(r["ns_per_call"], 1.0)))
    if not scores:
        return 0.0
    product = 1.0
    for s in scores:
        product *= max(s, 1e-6)
    score = product ** (1.0 / len(scores))
    if not memory.get("within_budget", True):
        score *= 0.1
    return round(score, 6)


def main():
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

    print("Kernel Results:", file=sys.stderr)
    hdr = f"{'kernel':<10} {'accuracy':>10} {'ns/call':>10} {'errors':>7}"
    print(hdr, file=sys.stderr)
    print("-" * 40, file=sys.stderr)
    for name, r in results.items():
        line = f"{name:<10} {r['accuracy']:>10.6f} {r['ns_per_call']:>10.1f} {r['errors']:>7}"
        print(line, file=sys.stderr)
    budget_status = "OK" if memory["within_budget"] else "OVER BUDGET!"
    print(f"Memory: {memory['total_bytes']}/{memory['budget_bytes']} bytes ({budget_status})", file=sys.stderr)
    print(f"Composite score: {score}", file=sys.stderr)

    print(json.dumps(output))


if __name__ == "__main__":
    main()
'''

# ═══════════════════════════════════════════════════════════════════════════
# Baseline kernels.py — slow but correct (score ~0.09 from geo mean of
# acc*0.1 per kernel since all are < 99.99% accuracy due to slowness)
# ═══════════════════════════════════════════════════════════════════════════

BASELINE_KERNELS = '''\
"""Fast math kernels — correct but slow implementations to be optimized.

Constraints:
  - MEMORY_BUDGET: total bytes for all lookup tables combined (4096 bytes)
  - No math stdlib (math.sin, math.exp, etc.)
  - No numpy/scipy
  - Pure Python arithmetic + struct for bit tricks
  - Must handle the full input range specified in eval_harness.py
  - Accuracy target: 99.99% relative accuracy (max_rel_error < 0.0001)

Optimization levers:
  - Allocate TABLES memory between functions (tradeoff: more table = faster but less for others)
  - Choose polynomial degree vs table+interpolation vs hybrid
  - Exploit function dependencies: sigmoid uses exp, tanh uses sigmoid
  - Range reduction to minimize table size needed
  - Bit tricks via struct.pack/unpack for initial guesses

The TABLES dict below is pre-allocated at import time. Each value is a list
of floats. Total memory = sum(len(t) * 8 for t in TABLES.values()) must be
<= MEMORY_BUDGET. The eval harness enforces this.
"""
import struct

# Shared memory budget for ALL lookup tables (in bytes, 8 bytes per float)
MEMORY_BUDGET = 4096  # = 512 floats total across all tables

# Pre-allocated lookup tables — agent distributes budget here
# Total floats across all tables must be <= MEMORY_BUDGET // 8 = 512
TABLES: dict[str, list[float]] = {
    # Example: "sin": [precomputed values...],
    # Start empty — agent allocates as needed
}


def fast_sin(x: float) -> float:
    """Compute sin(x) for x in [-2pi, 2pi]. Target: 99.99% accuracy."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586

    # Range reduce to [-pi, pi]
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI

    # Polynomial approximation (many terms for accuracy — slow)
    x2 = x * x
    # Taylor series with 9 terms
    result = x
    term = x
    for n in range(1, 9):
        term *= -x2 / ((2 * n) * (2 * n + 1))
        result += term
    return result


def fast_cos(x: float) -> float:
    """Compute cos(x) for x in [-2pi, 2pi]. Target: 99.99% accuracy."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586

    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI

    x2 = x * x
    result = 1.0
    term = 1.0
    for n in range(1, 9):
        term *= -x2 / ((2 * n - 1) * (2 * n))
        result += term
    return result


def fast_exp(x: float) -> float:
    """Compute e^x for x in [-10, 10]. Target: 99.99% accuracy."""
    if x > 10:
        x = 10.0
    if x < -10:
        x = -10.0

    # Range reduction: e^x = 2^k * e^r where |r| <= ln2/2
    LN2 = 0.6931471805599453
    k = int(x / LN2 + (0.5 if x >= 0 else -0.5))
    r = x - k * LN2

    # Taylor series for e^r (slow — many terms needed)
    result = 1.0
    term = 1.0
    for n in range(1, 14):
        term *= r / n
        result += term

    # Multiply by 2^k
    if k >= 0:
        for _ in range(k):
            result *= 2.0
    else:
        for _ in range(-k):
            result *= 0.5

    return result


def fast_log(x: float) -> float:
    """Compute ln(x) for x in (0, 1000]. Target: 99.99% accuracy."""
    if x <= 0:
        return float("-inf")

    # Reduce to [1, 2): x = m * 2^e
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1

    # ln(m) via atanh series (faster convergence than naive Taylor)
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)

    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Compute sqrt(x) for x in [0, 1e6]. Target: 99.99% accuracy."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0

    # Initial guess via bit manipulation
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2

    # Newton iterations (many for 99.99% accuracy)
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Compute 1/(1+e^-x) for x in [-20, 20]. Target: 99.99% accuracy."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Compute tanh(x) for x in [-10, 10]. Target: 99.99% accuracy."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''


# ═══════════════════════════════════════════════════════════════════════════
# Per-idea kernel implementations
# ═══════════════════════════════════════════════════════════════════════════
# Each idea modifies kernels.py. We store the full file content for each.

# Helper: precomputed sin table generation code (used in table-heavy ideas)
def _make_sin_table(n: int) -> list[float]:
    """Generate sin table values for [0, 2*pi)."""
    import math
    return [math.sin(2 * math.pi * i / n) for i in range(n)]


def _make_exp_table(n: int, lo: float, hi: float) -> list[float]:
    """Generate exp table values for [lo, hi]."""
    import math
    step = (hi - lo) / (n - 1)
    return [math.exp(lo + i * step) for i in range(n)]


def _make_log_table(n: int, lo: float, hi: float) -> list[float]:
    """Generate log table values for [lo, hi]."""
    import math
    step = (hi - lo) / (n - 1)
    return [math.log(lo + i * step) for i in range(n)]


# ──────────────────────────────────────────────────────────────────────────
# IDEA 1: 64-entry sin table with linear interpolation (512 bytes)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_1 = '''\
"""Idea 1: 64-entry sin table with linear interpolation.

Uses 512 bytes (64 floats * 8 bytes). Other kernels unchanged (still slow).
"""
import struct
import math as _m  # only used for table generation at import time

MEMORY_BUDGET = 4096

# 64-entry sin table for [0, 2*pi)
_SIN_N = 64
TABLES: dict[str, list[float]] = {
    "sin": [_m.sin(2 * _m.pi * i / _SIN_N) for i in range(_SIN_N)],
}


def fast_sin(x: float) -> float:
    """Sin via table lookup + linear interpolation."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    table = TABLES["sin"]
    n = _SIN_N

    # Normalize to [0, 2*pi)
    x = x % TWO_PI
    if x < 0:
        x += TWO_PI

    # Table index
    idx_f = x / TWO_PI * n
    idx = int(idx_f)
    frac = idx_f - idx
    idx = idx % n
    idx_next = (idx + 1) % n

    return table[idx] * (1.0 - frac) + table[idx_next] * frac


def fast_cos(x: float) -> float:
    """Cos via Taylor series (unchanged — still slow)."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    result = 1.0
    term = 1.0
    for n in range(1, 9):
        term *= -x2 / ((2 * n - 1) * (2 * n))
        result += term
    return result


def fast_exp(x: float) -> float:
    """Exp via Taylor series (unchanged — still slow)."""
    if x > 10:
        x = 10.0
    if x < -10:
        x = -10.0
    LN2 = 0.6931471805599453
    k = int(x / LN2 + (0.5 if x >= 0 else -0.5))
    r = x - k * LN2
    result = 1.0
    term = 1.0
    for n in range(1, 14):
        term *= r / n
        result += term
    if k >= 0:
        for _ in range(k):
            result *= 2.0
    else:
        for _ in range(-k):
            result *= 0.5
    return result


def fast_log(x: float) -> float:
    """Log via atanh series (unchanged — still slow)."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via Newton (unchanged — still slow)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid (unchanged)."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh (unchanged)."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 2: Add 64-entry exp table (branch from 1, 1024 bytes total)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_2 = '''\
"""Idea 2: 64-entry sin table + 64-entry exp table.

Uses 1024 bytes total (128 floats * 8). Sin and exp use tables, rest unchanged.
"""
import struct
import math as _m

MEMORY_BUDGET = 4096

_SIN_N = 64
_EXP_N = 64
_EXP_LO, _EXP_HI = -10.0, 10.0

TABLES: dict[str, list[float]] = {
    "sin": [_m.sin(2 * _m.pi * i / _SIN_N) for i in range(_SIN_N)],
    "exp": [_m.exp(_EXP_LO + i * (_EXP_HI - _EXP_LO) / (_EXP_N - 1)) for i in range(_EXP_N)],
}


def fast_sin(x: float) -> float:
    """Sin via 64-entry table + lerp."""
    TWO_PI = 6.283185307179586
    table = TABLES["sin"]
    n = _SIN_N
    x = x % TWO_PI
    if x < 0:
        x += TWO_PI
    idx_f = x / TWO_PI * n
    idx = int(idx_f)
    frac = idx_f - idx
    idx = idx % n
    return table[idx] * (1.0 - frac) + table[(idx + 1) % n] * frac


def fast_cos(x: float) -> float:
    """Cos via Taylor (unchanged)."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    result = 1.0
    term = 1.0
    for n in range(1, 9):
        term *= -x2 / ((2 * n - 1) * (2 * n))
        result += term
    return result


def fast_exp(x: float) -> float:
    """Exp via 64-entry table + lerp."""
    if x > 10.0:
        x = 10.0
    if x < -10.0:
        x = -10.0
    table = TABLES["exp"]
    n = _EXP_N
    idx_f = (x - _EXP_LO) / (_EXP_HI - _EXP_LO) * (n - 1)
    idx = int(idx_f)
    if idx < 0:
        idx = 0
    if idx >= n - 1:
        return table[n - 1]
    frac = idx_f - idx
    return table[idx] * (1.0 - frac) + table[idx + 1] * frac


def fast_log(x: float) -> float:
    """Log via atanh series (unchanged)."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via Newton (unchanged)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid using table-accelerated exp."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh using table-accelerated sigmoid."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 3: 128-entry sin table (branch from 1, better accuracy, 1024 bytes)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_3 = '''\
"""Idea 3: 128-entry sin table for better accuracy.

Uses 1024 bytes (128 floats * 8). More table entries => better interpolation.
"""
import struct
import math as _m

MEMORY_BUDGET = 4096

_SIN_N = 128
TABLES: dict[str, list[float]] = {
    "sin": [_m.sin(2 * _m.pi * i / _SIN_N) for i in range(_SIN_N)],
}


def fast_sin(x: float) -> float:
    """Sin via 128-entry table + lerp."""
    TWO_PI = 6.283185307179586
    table = TABLES["sin"]
    n = _SIN_N
    x = x % TWO_PI
    if x < 0:
        x += TWO_PI
    idx_f = x / TWO_PI * n
    idx = int(idx_f)
    frac = idx_f - idx
    idx = idx % n
    return table[idx] * (1.0 - frac) + table[(idx + 1) % n] * frac


def fast_cos(x: float) -> float:
    """Cos via Taylor (unchanged)."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    result = 1.0
    term = 1.0
    for n in range(1, 9):
        term *= -x2 / ((2 * n - 1) * (2 * n))
        result += term
    return result


def fast_exp(x: float) -> float:
    """Exp via Taylor (unchanged)."""
    if x > 10:
        x = 10.0
    if x < -10:
        x = -10.0
    LN2 = 0.6931471805599453
    k = int(x / LN2 + (0.5 if x >= 0 else -0.5))
    r = x - k * LN2
    result = 1.0
    term = 1.0
    for n in range(1, 14):
        term *= r / n
        result += term
    if k >= 0:
        for _ in range(k):
            result *= 2.0
    else:
        for _ in range(-k):
            result *= 0.5
    return result


def fast_log(x: float) -> float:
    """Log via atanh series (unchanged)."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via Newton (unchanged)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid (unchanged)."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh (unchanged)."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 4: Cos table reusing sin (branch from 3, cos=sin(x+pi/2))
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_4 = '''\
"""Idea 4: 128-entry sin table, cos reuses it via phase shift.

cos(x) = sin(x + pi/2). Same 1024 bytes, but now two kernels are fast.
"""
import struct
import math as _m

MEMORY_BUDGET = 4096

_SIN_N = 128
TABLES: dict[str, list[float]] = {
    "sin": [_m.sin(2 * _m.pi * i / _SIN_N) for i in range(_SIN_N)],
}


def _sin_table_lookup(x: float) -> float:
    """Shared sin table lookup."""
    TWO_PI = 6.283185307179586
    table = TABLES["sin"]
    n = _SIN_N
    x = x % TWO_PI
    if x < 0:
        x += TWO_PI
    idx_f = x / TWO_PI * n
    idx = int(idx_f)
    frac = idx_f - idx
    idx = idx % n
    return table[idx] * (1.0 - frac) + table[(idx + 1) % n] * frac


def fast_sin(x: float) -> float:
    """Sin via 128-entry table."""
    return _sin_table_lookup(x)


def fast_cos(x: float) -> float:
    """Cos via sin table: cos(x) = sin(x + pi/2)."""
    PI_HALF = 1.5707963267948966
    return _sin_table_lookup(x + PI_HALF)


def fast_exp(x: float) -> float:
    """Exp via Taylor (unchanged)."""
    if x > 10:
        x = 10.0
    if x < -10:
        x = -10.0
    LN2 = 0.6931471805599453
    k = int(x / LN2 + (0.5 if x >= 0 else -0.5))
    r = x - k * LN2
    result = 1.0
    term = 1.0
    for n in range(1, 14):
        term *= r / n
        result += term
    if k >= 0:
        for _ in range(k):
            result *= 2.0
    else:
        for _ in range(-k):
            result *= 0.5
    return result


def fast_log(x: float) -> float:
    """Log via atanh series (unchanged)."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via Newton (unchanged)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid (unchanged)."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh (unchanged)."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 5: Add 32-entry log table (branch from 2, 1280 bytes total)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_5 = '''\
"""Idea 5: Sin + exp + log tables. 1280 bytes total.

64 sin + 64 exp + 32 log = 160 floats = 1280 bytes.
"""
import struct
import math as _m

MEMORY_BUDGET = 4096

_SIN_N = 64
_EXP_N = 64
_EXP_LO, _EXP_HI = -10.0, 10.0
_LOG_N = 32
_LOG_LO, _LOG_HI = 0.001, 1000.0

TABLES: dict[str, list[float]] = {
    "sin": [_m.sin(2 * _m.pi * i / _SIN_N) for i in range(_SIN_N)],
    "exp": [_m.exp(_EXP_LO + i * (_EXP_HI - _EXP_LO) / (_EXP_N - 1)) for i in range(_EXP_N)],
    "log": [_m.log(_LOG_LO + i * (_LOG_HI - _LOG_LO) / (_LOG_N - 1)) for i in range(_LOG_N)],
}


def fast_sin(x: float) -> float:
    """Sin via 64-entry table + lerp."""
    TWO_PI = 6.283185307179586
    table = TABLES["sin"]
    n = _SIN_N
    x = x % TWO_PI
    if x < 0:
        x += TWO_PI
    idx_f = x / TWO_PI * n
    idx = int(idx_f)
    frac = idx_f - idx
    idx = idx % n
    return table[idx] * (1.0 - frac) + table[(idx + 1) % n] * frac


def fast_cos(x: float) -> float:
    """Cos via Taylor (unchanged)."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    result = 1.0
    term = 1.0
    for n in range(1, 9):
        term *= -x2 / ((2 * n - 1) * (2 * n))
        result += term
    return result


def fast_exp(x: float) -> float:
    """Exp via 64-entry table + lerp."""
    if x > 10.0:
        x = 10.0
    if x < -10.0:
        x = -10.0
    table = TABLES["exp"]
    n = _EXP_N
    idx_f = (x - _EXP_LO) / (_EXP_HI - _EXP_LO) * (n - 1)
    idx = int(idx_f)
    if idx < 0:
        idx = 0
    if idx >= n - 1:
        return table[n - 1]
    frac = idx_f - idx
    return table[idx] * (1.0 - frac) + table[idx + 1] * frac


def fast_log(x: float) -> float:
    """Log via 32-entry table + lerp."""
    if x <= 0:
        return float("-inf")
    if x < _LOG_LO:
        x = _LOG_LO
    if x > _LOG_HI:
        x = _LOG_HI
    table = TABLES["log"]
    n = _LOG_N
    idx_f = (x - _LOG_LO) / (_LOG_HI - _LOG_LO) * (n - 1)
    idx = int(idx_f)
    if idx < 0:
        idx = 0
    if idx >= n - 1:
        return table[n - 1]
    frac = idx_f - idx
    return table[idx] * (1.0 - frac) + table[idx + 1] * frac


def fast_sqrt(x: float) -> float:
    """Sqrt via Newton (unchanged)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid using table-accelerated exp."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh using sigmoid."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 6: Chebyshev sin/cos (no tables)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_6 = '''\
"""Idea 6: Chebyshev polynomial approximations for sin/cos.

No tables used. Chebyshev gives better accuracy per term than Taylor.
"""
import struct

MEMORY_BUDGET = 4096
TABLES: dict[str, list[float]] = {}


def fast_sin(x: float) -> float:
    """Sin via Chebyshev-derived minimax polynomial on [-pi, pi]."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    # Horner form of degree-7 minimax polynomial for sin on [-pi, pi]
    x2 = x * x
    # Coefficients from Chebyshev economization
    return x * (1.0 + x2 * (-0.16666666666666666 + x2 * (0.008333333333333333 + x2 * (-0.0001984126984126984 + x2 * 2.7557319223985893e-06))))


def fast_cos(x: float) -> float:
    """Cos via Chebyshev-derived minimax polynomial on [-pi, pi]."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    return 1.0 + x2 * (-0.5 + x2 * (0.041666666666666664 + x2 * (-0.001388888888888889 + x2 * 2.48015873015873e-05)))


def fast_exp(x: float) -> float:
    """Exp via Taylor (unchanged)."""
    if x > 10:
        x = 10.0
    if x < -10:
        x = -10.0
    LN2 = 0.6931471805599453
    k = int(x / LN2 + (0.5 if x >= 0 else -0.5))
    r = x - k * LN2
    result = 1.0
    term = 1.0
    for n in range(1, 14):
        term *= r / n
        result += term
    if k >= 0:
        for _ in range(k):
            result *= 2.0
    else:
        for _ in range(-k):
            result *= 0.5
    return result


def fast_log(x: float) -> float:
    """Log via atanh series (unchanged)."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via Newton (unchanged)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid (unchanged)."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh (unchanged)."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 7: Pade approximation for exp (branch from 6)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_7 = '''\
"""Idea 7: Chebyshev sin/cos + Pade exp.

Pade [4/4] approximation converges faster than Taylor for exp(r).
"""
import struct

MEMORY_BUDGET = 4096
TABLES: dict[str, list[float]] = {}


def fast_sin(x: float) -> float:
    """Sin via Chebyshev polynomial."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    return x * (1.0 + x2 * (-0.16666666666666666 + x2 * (0.008333333333333333 + x2 * (-0.0001984126984126984 + x2 * 2.7557319223985893e-06))))


def fast_cos(x: float) -> float:
    """Cos via Chebyshev polynomial."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    return 1.0 + x2 * (-0.5 + x2 * (0.041666666666666664 + x2 * (-0.001388888888888889 + x2 * 2.48015873015873e-05)))


def fast_exp(x: float) -> float:
    """Exp via range reduction + Pade [4/4] approximation."""
    if x > 10:
        x = 10.0
    if x < -10:
        x = -10.0
    LN2 = 0.6931471805599453
    k = int(x / LN2 + (0.5 if x >= 0 else -0.5))
    r = x - k * LN2
    # Pade [4/4] for e^r near 0
    r2 = r * r
    num = 1.0 + r * 0.5 + r2 * (1.0 / 12.0)
    den = 1.0 - r * 0.5 + r2 * (1.0 / 12.0)
    result = num / den
    # Multiply by 2^k using bit manipulation
    if k >= 0:
        for _ in range(k):
            result *= 2.0
    else:
        for _ in range(-k):
            result *= 0.5
    return result


def fast_log(x: float) -> float:
    """Log via atanh series (unchanged)."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via Newton (unchanged)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid using Pade exp."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh using sigmoid."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 8: Improved sqrt with bit trick + fewer Newton steps (branch from 6)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_8 = '''\
"""Idea 8: Chebyshev sin/cos + improved sqrt via better initial guess.

Bit trick gives a good initial guess, so 3 Newton steps suffice.
"""
import struct

MEMORY_BUDGET = 4096
TABLES: dict[str, list[float]] = {}


def fast_sin(x: float) -> float:
    """Sin via Chebyshev polynomial."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    return x * (1.0 + x2 * (-0.16666666666666666 + x2 * (0.008333333333333333 + x2 * (-0.0001984126984126984 + x2 * 2.7557319223985893e-06))))


def fast_cos(x: float) -> float:
    """Cos via Chebyshev polynomial."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    return 1.0 + x2 * (-0.5 + x2 * (0.041666666666666664 + x2 * (-0.001388888888888889 + x2 * 2.48015873015873e-05)))


def fast_exp(x: float) -> float:
    """Exp via Taylor (unchanged from baseline)."""
    if x > 10:
        x = 10.0
    if x < -10:
        x = -10.0
    LN2 = 0.6931471805599453
    k = int(x / LN2 + (0.5 if x >= 0 else -0.5))
    r = x - k * LN2
    result = 1.0
    term = 1.0
    for n in range(1, 14):
        term *= r / n
        result += term
    if k >= 0:
        for _ in range(k):
            result *= 2.0
    else:
        for _ in range(-k):
            result *= 0.5
    return result


def fast_log(x: float) -> float:
    """Log via atanh series (unchanged)."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via bit trick + 3 Newton iterations (much faster)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    # Good initial guess via IEEE754 bit trick
    bits = struct.unpack('Q', struct.pack('d', x))[0]
    bits = (bits >> 1) + 0x1FF8000000000000
    guess = struct.unpack('d', struct.pack('Q', bits))[0]
    # Only 3 Newton iterations needed with good initial guess
    guess = 0.5 * (guess + x / guess)
    guess = 0.5 * (guess + x / guess)
    guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid (unchanged)."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh (unchanged)."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 9: Better sigmoid clamping (branch from 7)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_9 = '''\
"""Idea 9: Chebyshev sin/cos + Pade exp + better sigmoid/tanh clamping.

Tighter clamp ranges and algebraic identity for better accuracy near edges.
"""
import struct

MEMORY_BUDGET = 4096
TABLES: dict[str, list[float]] = {}


def fast_sin(x: float) -> float:
    """Sin via Chebyshev polynomial."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    return x * (1.0 + x2 * (-0.16666666666666666 + x2 * (0.008333333333333333 + x2 * (-0.0001984126984126984 + x2 * 2.7557319223985893e-06))))


def fast_cos(x: float) -> float:
    """Cos via Chebyshev polynomial."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    return 1.0 + x2 * (-0.5 + x2 * (0.041666666666666664 + x2 * (-0.001388888888888889 + x2 * 2.48015873015873e-05)))


def fast_exp(x: float) -> float:
    """Exp via Pade [4/4]."""
    if x > 10:
        x = 10.0
    if x < -10:
        x = -10.0
    LN2 = 0.6931471805599453
    k = int(x / LN2 + (0.5 if x >= 0 else -0.5))
    r = x - k * LN2
    r2 = r * r
    num = 1.0 + r * 0.5 + r2 * (1.0 / 12.0)
    den = 1.0 - r * 0.5 + r2 * (1.0 / 12.0)
    result = num / den
    if k >= 0:
        for _ in range(k):
            result *= 2.0
    else:
        for _ in range(-k):
            result *= 0.5
    return result


def fast_log(x: float) -> float:
    """Log via atanh series (unchanged)."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via Newton (unchanged — still 8 iters)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid with numerically stable form for full range."""
    if x >= 0:
        ex = fast_exp(-x)
        return 1.0 / (1.0 + ex)
    else:
        ex = fast_exp(x)
        return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh with direct computation for small x."""
    if x > 10:
        return 1.0
    if x < -10:
        return -1.0
    # For small |x|, tanh(x) ~ x - x^3/3 + ...
    if -0.01 < x < 0.01:
        x2 = x * x
        return x * (1.0 - x2 * (1.0/3.0 - x2 * (2.0/15.0)))
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 10: Best polynomial — all kernels improved (branch from 9)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_10 = '''\
"""Idea 10: Best polynomial approach — all kernels use fast approximations.

Chebyshev sin/cos, Pade exp, fast Newton sqrt, improved log, tight sigmoid.
No tables used.
"""
import struct

MEMORY_BUDGET = 4096
TABLES: dict[str, list[float]] = {}


def fast_sin(x: float) -> float:
    """Sin via Chebyshev polynomial on [-pi, pi]."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    return x * (1.0 + x2 * (-0.16666666666666666 + x2 * (0.008333333333333333 + x2 * (-0.0001984126984126984 + x2 * 2.7557319223985893e-06))))


def fast_cos(x: float) -> float:
    """Cos via Chebyshev polynomial on [-pi, pi]."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    return 1.0 + x2 * (-0.5 + x2 * (0.041666666666666664 + x2 * (-0.001388888888888889 + x2 * 2.48015873015873e-05)))


def fast_exp(x: float) -> float:
    """Exp via range reduction + Pade [6/6]."""
    if x > 10:
        x = 10.0
    if x < -10:
        x = -10.0
    LN2 = 0.6931471805599453
    k = int(x / LN2 + (0.5 if x >= 0 else -0.5))
    r = x - k * LN2
    # Pade [6/6] for e^r
    r2 = r * r
    r3 = r2 * r
    num = 1.0 + r / 2.0 + r2 / 10.0 + r3 / 120.0
    den = 1.0 - r / 2.0 + r2 / 10.0 - r3 / 120.0
    result = num / den
    if k >= 0:
        for _ in range(k):
            result *= 2.0
    else:
        for _ in range(-k):
            result *= 0.5
    return result


def fast_log(x: float) -> float:
    """Log via range reduction + faster Pade-like series."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    # Use (m-1)/(m+1) form with fewer terms (converges faster)
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    # Horner form: 2*(y + y^3/3 + y^5/5 + y^7/7 + y^9/9)
    result = y * (1.0 + y2 * (1.0/3.0 + y2 * (1.0/5.0 + y2 * (1.0/7.0 + y2 * (1.0/9.0 + y2 * (1.0/11.0))))))
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via bit trick + 3 Newton iterations."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    bits = struct.unpack('Q', struct.pack('d', x))[0]
    bits = (bits >> 1) + 0x1FF8000000000000
    guess = struct.unpack('d', struct.pack('Q', bits))[0]
    guess = 0.5 * (guess + x / guess)
    guess = 0.5 * (guess + x / guess)
    guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid with accurate computation for full range."""
    if x >= 0:
        ex = fast_exp(-x)
        return 1.0 / (1.0 + ex)
    else:
        ex = fast_exp(x)
        return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh with small-x optimization."""
    if x > 10:
        return 1.0
    if x < -10:
        return -1.0
    if -0.01 < x < 0.01:
        x2 = x * x
        return x * (1.0 - x2 * (1.0/3.0 - x2 * (2.0/15.0)))
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 11: Small 16-entry exp table + Pade correction (128 bytes)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_11 = '''\
"""Idea 11: Hybrid approach — small 16-entry exp table + Pade correction.

The table gives a coarse exp value, then Pade refines the residual.
Only 128 bytes. This gives much better exp accuracy than pure polynomial.
"""
import struct
import math as _m

MEMORY_BUDGET = 4096

_EXP_N = 16
_EXP_LO, _EXP_HI = -10.0, 10.0
_EXP_STEP = (_EXP_HI - _EXP_LO) / (_EXP_N - 1)

TABLES: dict[str, list[float]] = {
    "exp": [_m.exp(_EXP_LO + i * _EXP_STEP) for i in range(_EXP_N)],
}


def fast_sin(x: float) -> float:
    """Sin via Taylor (unchanged for now)."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    result = x
    term = x
    for n in range(1, 9):
        term *= -x2 / ((2 * n) * (2 * n + 1))
        result += term
    return result


def fast_cos(x: float) -> float:
    """Cos via Taylor (unchanged for now)."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI
    x2 = x * x
    result = 1.0
    term = 1.0
    for n in range(1, 9):
        term *= -x2 / ((2 * n - 1) * (2 * n))
        result += term
    return result


def fast_exp(x: float) -> float:
    """Exp via table lookup + Pade correction for residual."""
    if x > 10.0:
        x = 10.0
    if x < -10.0:
        x = -10.0
    table = TABLES["exp"]
    # Find nearest table entry
    idx_f = (x - _EXP_LO) / _EXP_STEP
    idx = int(idx_f)
    if idx < 0:
        idx = 0
    if idx >= _EXP_N - 1:
        idx = _EXP_N - 2
    # Residual: x = x_table + r
    x_table = _EXP_LO + idx * _EXP_STEP
    r = x - x_table
    # exp(x) = exp(x_table) * exp(r), use Pade [2/2] for exp(r)
    r2 = r * r
    exp_r = (1.0 + r * 0.5 + r2 / 12.0) / (1.0 - r * 0.5 + r2 / 12.0)
    return table[idx] * exp_r


def fast_log(x: float) -> float:
    """Log via atanh series (unchanged)."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via Newton (unchanged)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid using hybrid exp."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh using sigmoid."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 12: Small sin/cos table + Chebyshev (branch from 11, 384 bytes)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_12 = '''\
"""Idea 12: Hybrid — small exp table + small sin table + Chebyshev refinement.

16-entry exp table (128 bytes) + 32-entry sin table (256 bytes) = 384 bytes.
Sin uses table + cubic correction, cos reuses sin table.
"""
import struct
import math as _m

MEMORY_BUDGET = 4096

_EXP_N = 16
_EXP_LO, _EXP_HI = -10.0, 10.0
_EXP_STEP = (_EXP_HI - _EXP_LO) / (_EXP_N - 1)
_SIN_N = 32

TABLES: dict[str, list[float]] = {
    "exp": [_m.exp(_EXP_LO + i * _EXP_STEP) for i in range(_EXP_N)],
    "sin": [_m.sin(2 * _m.pi * i / _SIN_N) for i in range(_SIN_N)],
}


def _sin_hybrid(x: float) -> float:
    """Sin via small table + cubic correction."""
    TWO_PI = 6.283185307179586
    table = TABLES["sin"]
    n = _SIN_N
    x = x % TWO_PI
    if x < 0:
        x += TWO_PI
    idx_f = x / TWO_PI * n
    idx = int(idx_f)
    frac = idx_f - idx
    idx = idx % n
    idx_next = (idx + 1) % n
    # Linear interpolation + cubic correction for better accuracy
    v0 = table[idx]
    v1 = table[idx_next]
    # Hermite-like: lerp + correction
    lerp = v0 * (1.0 - frac) + v1 * frac
    # Small correction: frac*(1-frac) peaks at 0.5
    correction = frac * (1.0 - frac) * (v1 - v0) * 0.1
    return lerp + correction


def fast_sin(x: float) -> float:
    """Sin via hybrid table + correction."""
    return _sin_hybrid(x)


def fast_cos(x: float) -> float:
    """Cos via sin table + phase shift."""
    PI_HALF = 1.5707963267948966
    return _sin_hybrid(x + PI_HALF)


def fast_exp(x: float) -> float:
    """Exp via table + Pade correction."""
    if x > 10.0:
        x = 10.0
    if x < -10.0:
        x = -10.0
    table = TABLES["exp"]
    idx_f = (x - _EXP_LO) / _EXP_STEP
    idx = int(idx_f)
    if idx < 0:
        idx = 0
    if idx >= _EXP_N - 1:
        idx = _EXP_N - 2
    x_table = _EXP_LO + idx * _EXP_STEP
    r = x - x_table
    r2 = r * r
    exp_r = (1.0 + r * 0.5 + r2 / 12.0) / (1.0 - r * 0.5 + r2 / 12.0)
    return table[idx] * exp_r


def fast_log(x: float) -> float:
    """Log via atanh series (unchanged)."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via Newton (unchanged)."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + 0x1FF8000000000000
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid using hybrid exp."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh using sigmoid."""
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 13: All kernels hybrid — OVERALL BEST (~2KB, score ~0.75)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_13 = '''\
"""Idea 13: Full hybrid — all kernels use table + polynomial correction.

Tables: 32 sin + 32 exp + 32 log = 96 floats * 8 = 768 bytes (well under budget).
All kernels use: small table for coarse value, polynomial for refinement.
This is the best overall approach.
"""
import struct
import math as _m

MEMORY_BUDGET = 4096

_SIN_N = 32
_EXP_N = 32
_EXP_LO, _EXP_HI = -10.0, 10.0
_EXP_STEP = (_EXP_HI - _EXP_LO) / (_EXP_N - 1)
_LOG_N = 32
_LOG_LO, _LOG_HI = 1.0, 2.0  # log only needs [1,2) after range reduction

TABLES: dict[str, list[float]] = {
    "sin": [_m.sin(2 * _m.pi * i / _SIN_N) for i in range(_SIN_N)],
    "exp": [_m.exp(_EXP_LO + i * _EXP_STEP) for i in range(_EXP_N)],
    "log": [_m.log(_LOG_LO + i * (_LOG_HI - _LOG_LO) / (_LOG_N - 1)) for i in range(_LOG_N)],
}


def fast_sin(x: float) -> float:
    """Sin via 32-entry table + cubic Hermite correction."""
    TWO_PI = 6.283185307179586
    table = TABLES["sin"]
    n = _SIN_N
    x = x % TWO_PI
    if x < 0:
        x += TWO_PI
    idx_f = x / TWO_PI * n
    idx = int(idx_f)
    frac = idx_f - idx
    idx = idx % n
    idx_next = (idx + 1) % n
    v0 = table[idx]
    v1 = table[idx_next]
    # Cubic Hermite with derivative from table neighbors
    idx_prev = (idx - 1) % n
    idx_nn = (idx + 2) % n
    m0 = (v1 - table[idx_prev]) * 0.5
    m1 = (table[idx_nn] - v0) * 0.5
    t = frac
    t2 = t * t
    t3 = t2 * t
    return (2*t3 - 3*t2 + 1)*v0 + (t3 - 2*t2 + t)*m0 + (-2*t3 + 3*t2)*v1 + (t3 - t2)*m1


def fast_cos(x: float) -> float:
    """Cos via sin table: cos(x) = sin(x + pi/2)."""
    PI_HALF = 1.5707963267948966
    return fast_sin(x + PI_HALF)


def fast_exp(x: float) -> float:
    """Exp via table + Pade [2/2] correction for residual."""
    if x > 10.0:
        x = 10.0
    if x < -10.0:
        x = -10.0
    table = TABLES["exp"]
    idx_f = (x - _EXP_LO) / _EXP_STEP
    idx = int(idx_f)
    if idx < 0:
        idx = 0
    if idx >= _EXP_N - 1:
        idx = _EXP_N - 2
    x_table = _EXP_LO + idx * _EXP_STEP
    r = x - x_table
    r2 = r * r
    exp_r = (1.0 + r * 0.5 + r2 / 12.0) / (1.0 - r * 0.5 + r2 / 12.0)
    return table[idx] * exp_r


def fast_log(x: float) -> float:
    """Log via range reduction to [1,2) + table + polynomial."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    # Table lookup for ln(m) where m in [1, 2)
    table = TABLES["log"]
    n = _LOG_N
    idx_f = (m - _LOG_LO) / (_LOG_HI - _LOG_LO) * (n - 1)
    idx = int(idx_f)
    if idx < 0:
        idx = 0
    if idx >= n - 1:
        return table[n - 1] + e * LN2
    frac = idx_f - idx
    ln_m = table[idx] * (1.0 - frac) + table[idx + 1] * frac
    return ln_m + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via bit trick + 3 Newton iterations."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    bits = struct.unpack('Q', struct.pack('d', x))[0]
    bits = (bits >> 1) + 0x1FF8000000000000
    guess = struct.unpack('d', struct.pack('Q', bits))[0]
    guess = 0.5 * (guess + x / guess)
    guess = 0.5 * (guess + x / guess)
    guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid using hybrid exp, full range."""
    if x >= 0:
        ex = fast_exp(-x)
        return 1.0 / (1.0 + ex)
    else:
        ex = fast_exp(x)
        return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh with small-x path."""
    if x > 10:
        return 1.0
    if x < -10:
        return -1.0
    if -0.01 < x < 0.01:
        x2 = x * x
        return x * (1.0 - x2 * (1.0/3.0 - x2 * (2.0/15.0)))
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 14: Aggressive tables — OVER BUDGET (4.5KB)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_14 = '''\
"""Idea 14: Aggressive table sizes — exceeds 4KB budget!

256 sin + 128 exp + 64 log + 128 sqrt_hint = 576 floats = 4608 bytes > 4096.
Great accuracy and speed, but the memory penalty kills the score.
"""
import struct
import math as _m

MEMORY_BUDGET = 4096

_SIN_N = 256
_EXP_N = 128
_EXP_LO, _EXP_HI = -10.0, 10.0
_EXP_STEP = (_EXP_HI - _EXP_LO) / (_EXP_N - 1)
_LOG_N = 64
_LOG_LO, _LOG_HI = 1.0, 2.0
_SQRT_N = 128
_SQRT_LO, _SQRT_HI = 0.25, 4.0  # after normalization

TABLES: dict[str, list[float]] = {
    "sin": [_m.sin(2 * _m.pi * i / _SIN_N) for i in range(_SIN_N)],
    "exp": [_m.exp(_EXP_LO + i * _EXP_STEP) for i in range(_EXP_N)],
    "log": [_m.log(_LOG_LO + i * (_LOG_HI - _LOG_LO) / (_LOG_N - 1)) for i in range(_LOG_N)],
    "sqrt": [_m.sqrt(_SQRT_LO + i * (_SQRT_HI - _SQRT_LO) / (_SQRT_N - 1)) for i in range(_SQRT_N)],
}


def fast_sin(x: float) -> float:
    """Sin via 256-entry table — excellent accuracy."""
    TWO_PI = 6.283185307179586
    table = TABLES["sin"]
    n = _SIN_N
    x = x % TWO_PI
    if x < 0:
        x += TWO_PI
    idx_f = x / TWO_PI * n
    idx = int(idx_f)
    frac = idx_f - idx
    idx = idx % n
    return table[idx] * (1.0 - frac) + table[(idx + 1) % n] * frac


def fast_cos(x: float) -> float:
    """Cos via sin table phase shift."""
    PI_HALF = 1.5707963267948966
    return fast_sin(x + PI_HALF)


def fast_exp(x: float) -> float:
    """Exp via 128-entry table + Pade."""
    if x > 10.0:
        x = 10.0
    if x < -10.0:
        x = -10.0
    table = TABLES["exp"]
    idx_f = (x - _EXP_LO) / _EXP_STEP
    idx = int(idx_f)
    if idx < 0:
        idx = 0
    if idx >= _EXP_N - 1:
        idx = _EXP_N - 2
    x_table = _EXP_LO + idx * _EXP_STEP
    r = x - x_table
    r2 = r * r
    exp_r = (1.0 + r * 0.5 + r2 / 12.0) / (1.0 - r * 0.5 + r2 / 12.0)
    return table[idx] * exp_r


def fast_log(x: float) -> float:
    """Log via range reduction + 64-entry table."""
    if x <= 0:
        return float("-inf")
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1
    LN2 = 0.6931471805599453
    table = TABLES["log"]
    n = _LOG_N
    idx_f = (m - _LOG_LO) / (_LOG_HI - _LOG_LO) * (n - 1)
    idx = int(idx_f)
    if idx < 0:
        idx = 0
    if idx >= n - 1:
        return table[n - 1] + e * LN2
    frac = idx_f - idx
    return (table[idx] * (1.0 - frac) + table[idx + 1] * frac) + e * LN2


def fast_sqrt(x: float) -> float:
    """Sqrt via table + Newton."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    # Normalize x to [0.25, 4): x = m * 4^e
    e = 0
    m = x
    while m >= 4.0:
        m *= 0.25
        e += 1
    while m < 0.25:
        m *= 4.0
        e -= 1
    table = TABLES["sqrt"]
    n = _SQRT_N
    idx_f = (m - _SQRT_LO) / (_SQRT_HI - _SQRT_LO) * (n - 1)
    idx = int(idx_f)
    if idx < 0:
        idx = 0
    if idx >= n - 1:
        guess = table[n - 1]
    else:
        frac = idx_f - idx
        guess = table[idx] * (1.0 - frac) + table[idx + 1] * frac
    # One Newton refinement
    guess = 0.5 * (guess + m / guess)
    # Scale back
    if e >= 0:
        for _ in range(e):
            guess *= 2.0
    else:
        for _ in range(-e):
            guess *= 0.5
    return guess


def fast_sigmoid(x: float) -> float:
    """Sigmoid using table exp."""
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Tanh using sigmoid."""
    if x > 10:
        return 1.0
    if x < -10:
        return -1.0
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
'''

# ──────────────────────────────────────────────────────────────────────────
# IDEA 15: Active idea, merging strategies A and C (no code changes yet)
# Uses the baseline kernels.py (same as main branch)
# ──────────────────────────────────────────────────────────────────────────

KERNELS_IDEA_15 = None  # no code changes, inherits from parent


# ═══════════════════════════════════════════════════════════════════════════
# FixtureBuilder class
# ═══════════════════════════════════════════════════════════════════════════

class FixtureBuilder:
    """Builds a pre-seeded Lab fixture directory."""

    def __init__(self, dest: Path):
        self.dest = dest
        self.lab_dir = dest / ".the_lab" / "experiments"
        self._next_exp_id = 1
        self._time_offset = 0.0

    def _tick(self, hours: float = 0.5) -> str:
        self._time_offset += hours
        return _ts(self._time_offset)

    def init_project(self, kernels_content: str = BASELINE_KERNELS):
        """Initialize git repo with math kernel project files."""
        self.dest.mkdir(parents=True, exist_ok=True)
        bench = self.dest / "benchmark"
        bench.mkdir(parents=True, exist_ok=True)

        (bench / "__init__.py").write_text(BENCHMARK_INIT)
        (bench / "reference.py").write_text(BENCHMARK_REFERENCE)
        (bench / "eval_harness.py").write_text(BENCHMARK_EVAL_HARNESS)
        (bench / "kernels.py").write_text(kernels_content)
        (self.dest / ".gitignore").write_text(".the_lab/\n__pycache__/\nbenchmark/__pycache__/\n")

        self._git("init")
        self._git("config", "user.name", "seed")
        self._git("config", "user.email", "seed@local")
        self._git("branch", "-m", "main")
        self._git("add", "-A")
        self._git("commit", "-m", "initial project: math kernel optimization")

    def add_idea(
        self,
        idea_id: int,
        description: str,
        parent_ids: list[int],
        status: str = "concluded",
        conclusion: str | None = None,
        kernels_content: str | None = None,
    ):
        """Create idea metadata and git branch."""
        idea_dir = self.lab_dir / str(idea_id)
        idea_dir.mkdir(parents=True, exist_ok=True)

        idea = {
            "id": idea_id,
            "description": description,
            "parent_ids": parent_ids,
            "status": status,
            "conclusion": conclusion,
            "branch": f"idea/{idea_id}",
            "source": "agent",
            "priority": "normal",
            "resources": [],
            "created_at": self._tick(),
        }
        (idea_dir / "idea.json").write_text(json.dumps(idea, indent=2))
        (idea_dir / "notes.json").write_text("[]")

        # Create git branch from parent
        parent_branch = f"idea/{parent_ids[0]}" if parent_ids else "main"
        self._git("branch", f"idea/{idea_id}", parent_branch)

        if kernels_content:
            self._git("checkout", f"idea/{idea_id}")
            (self.dest / "benchmark" / "kernels.py").write_text(kernels_content)
            self._git("add", "benchmark/kernels.py")
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"], cwd=str(self.dest),
                capture_output=True,
            )
            if result.returncode != 0:
                self._git("commit", "-m", f"idea {idea_id}: {description[:50]}")
            self._git("checkout", "main")

    def add_experiment(
        self,
        idea_id: int,
        description: str,
        status: str = "completed",
        metrics: dict | None = None,
        error: str | None = None,
        script_content: str = "#!/bin/bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython benchmark/eval_harness.py",
        log_content: str = "",
        err_content: str = "",
        tags: list[str] | None = None,
    ) -> int:
        """Add an experiment to an idea. Returns exp_id."""
        exp_id = self._next_exp_id
        self._next_exp_id += 1
        idea_dir = self.lab_dir / str(idea_id)

        existing = [f for f in idea_dir.iterdir() if f.suffix == ".json" and f.stem.isdigit()]
        seq = len(existing) + 1

        created = self._tick(0.1)
        started = self._tick(0.05) if status != "pending" else None
        finished = self._tick(0.2) if status in ("completed", "failed") else None

        exp = {
            "id": exp_id,
            "idea_id": idea_id,
            "seq": seq,
            "label": f"{idea_id}.{seq}",
            "description": description,
            "script": f".the_lab/experiments/{idea_id}/{exp_id}.sh",
            "status": status,
            "meta": {},
            "metrics": metrics,
            "error": error,
            "pid": None,
            "tags": tags or [],
            "created_at": created,
            "started_at": started,
            "finished_at": finished,
        }

        (idea_dir / f"{exp_id}.json").write_text(json.dumps(exp, indent=2))
        (idea_dir / f"{exp_id}.sh").write_text(script_content)
        if log_content:
            (idea_dir / f"{exp_id}.log").write_text(log_content)
        if err_content:
            (idea_dir / f"{exp_id}.err").write_text(err_content)
        return exp_id

    def add_note(self, idea_id: int, text: str, level: str = "observation"):
        """Append a note to an idea."""
        notes_path = self.lab_dir / str(idea_id) / "notes.json"
        notes = json.loads(notes_path.read_text()) if notes_path.exists() else []
        notes.append({"text": text, "level": level, "created_at": self._tick(0.05)})
        notes_path.write_text(json.dumps(notes, indent=2))

    def finalize(self):
        """Ensure main branch is checked out."""
        self._git("checkout", "main")

    def _git(self, *args):
        subprocess.run(
            ["git", *args], cwd=str(self.dest),
            capture_output=True, check=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Helper: generate realistic metrics/logs from idea descriptions
# ═══════════════════════════════════════════════════════════════════════════

def _metrics(score: float, mem_bytes: int, budget: int = 4096) -> dict:
    """Build a plausible metrics dict for a kernel experiment."""
    return {
        "score": score,
        "memory_used_bytes": mem_bytes,
        "memory_budget_bytes": budget,
        "memory_within_budget": mem_bytes <= budget,
    }


def _log_json(score: float, mem_bytes: int, budget: int = 4096) -> str:
    """Build a plausible JSON log line."""
    return json.dumps({
        "metrics": _metrics(score, mem_bytes, budget),
        "meta": {"kernels_tested": 7},
    })


# ═══════════════════════════════════════════════════════════════════════════
# Shared seed data: all 15 ideas with their kernel content
# ═══════════════════════════════════════════════════════════════════════════

# (idea_id, description, parent_ids, status, conclusion, kernels_content,
#  score, mem_bytes, tags, extra_notes)

SEED_IDEAS = [
    # --- Strategy A: Table-heavy (ideas 1-5) ---
    (1, "64-entry sin table with linear interpolation (512 bytes)",
     [], "concluded",
     "Table-based sin works. Score 0.30 — only sin benefits, other kernels still slow.",
     KERNELS_IDEA_1, 0.30, 512, ["table-heavy"],
     [("Sin table gives 1 fast kernel out of 7. Need more tables.", "observation")]),

    (2, "Add 64-entry exp table (shares sin table, 1024 bytes total)",
     [1], "concluded",
     "Sin + exp tables accelerate 4 kernels (sin, exp, sigmoid, tanh). Score 0.42.",
     KERNELS_IDEA_2, 0.42, 1024, ["table-heavy"],
     [("Exp table helps sigmoid and tanh too since they depend on exp.", "milestone")]),

    (3, "128-entry sin table for better accuracy (1024 bytes)",
     [1], "concluded",
     "Better sin accuracy with 128 entries but only sin benefits. Score 0.32.",
     KERNELS_IDEA_3, 0.32, 1024, ["table-heavy"],
     [("Doubling sin table size gives marginal accuracy gain. Not worth it alone.", "observation")]),

    (4, "Cos table reusing sin via phase shift (1024 bytes)",
     [3], "concluded",
     "cos(x) = sin(x + pi/2) works perfectly. Two fast trig kernels. Score 0.38.",
     KERNELS_IDEA_4, 0.38, 1024, ["table-heavy"],
     [("Smart reuse: cos from sin table with no extra memory.", "milestone")]),

    (5, "Add 32-entry log table (1280 bytes total)",
     [2], "concluded",
     "Sin + exp + log tables. Score 0.48 — more coverage but diminishing returns.",
     KERNELS_IDEA_5, 0.48, 1280, ["table-heavy"],
     [("Log table with linear interp has limited accuracy for wide range.", "observation"),
      ("Table-heavy approach plateauing. Need polynomial improvements too.", "observation")]),

    # --- Strategy B: Polynomial-only (ideas 6-10) ---
    (6, "Chebyshev polynomials for sin/cos (no tables)",
     [], "concluded",
     "Chebyshev gives better accuracy than Taylor with fewer terms. Score 0.22.",
     KERNELS_IDEA_6, 0.22, 0, ["polynomial"],
     [("Chebyshev: sin and cos fast and accurate, but exp/log/sqrt still slow.", "observation")]),

    (7, "Pade approximation for exp (branch from Chebyshev)",
     [6], "concluded",
     "Pade [4/4] for exp converges faster than Taylor. Score 0.35.",
     KERNELS_IDEA_7, 0.35, 0, ["polynomial"],
     [("Pade exp: faster convergence but [4/4] not accurate enough for 99.99%.", "observation")]),

    (8, "Improved sqrt with bit trick + 3 Newton steps",
     [6], "concluded",
     "Bit trick initial guess means only 3 Newton iterations needed. Score 0.28.",
     KERNELS_IDEA_8, 0.28, 0, ["polynomial"],
     [("Sqrt speedup significant: 8 iters -> 3 iters with better initial guess.", "milestone")]),

    (9, "Better sigmoid clamping for accuracy near saturation",
     [7], "concluded",
     "Tighter clamp ranges fix sigmoid accuracy at edges. Score 0.40.",
     KERNELS_IDEA_9, 0.40, 0, ["polynomial"],
     [("Sigmoid accuracy improved at edges. Tanh benefits from small-x optimization.", "observation")]),

    (10, "Combine all polynomial improvements — best polynomial approach",
     [9], "concluded",
     "All 7 kernels improved. Pade [6/6] exp, Chebyshev trig, fast sqrt. Score 0.55.",
     KERNELS_IDEA_10, 0.55, 0, ["polynomial"],
     [("Best pure-polynomial: score 0.55. No memory used. Could combine with tables.", "milestone"),
      ("Log still uses slow atanh loop. Main bottleneck now.", "observation")]),

    # --- Strategy C: Hybrid (ideas 11-15) ---
    (11, "Small 16-entry exp table + Pade correction (128 bytes)",
     [], "concluded",
     "Hybrid exp: table for coarse value, Pade for residual. Score 0.40.",
     KERNELS_IDEA_11, 0.40, 128, ["hybrid"],
     [("Hybrid exp is fast AND accurate. Only 128 bytes for huge improvement.", "milestone")]),

    (12, "Small sin/cos table + Chebyshev correction (384 bytes)",
     [11], "concluded",
     "Hybrid sin/cos + hybrid exp. Score 0.52.",
     KERNELS_IDEA_12, 0.52, 384, ["hybrid"],
     [("Three kernels now hybrid. Remaining: log, sqrt, sigmoid, tanh.", "observation")]),

    (13, "All kernels hybrid — tables + polynomial corrections (~768 bytes)",
     [12], "concluded",
     "Best overall: all kernels use small table + polynomial. Score 0.75. 768 bytes used.",
     KERNELS_IDEA_13, 0.75, 768, ["hybrid"],
     [("BEST SCORE SO FAR: 0.75. All 7 kernels fast and accurate.", "milestone"),
      ("Memory: 768/4096 bytes (19%). Plenty of headroom for larger tables.", "observation"),
      ("Could push further with larger tables but diminishing returns.", "observation")]),

    (14, "Aggressive table sizes — push for max speed (4608 bytes)",
     [13], "concluded",
     "OVER BUDGET: 4608 > 4096 bytes. Memory penalty destroys score. Score 0.08.",
     KERNELS_IDEA_14, None, 4608, ["hybrid", "over-budget"],
     [("CRITICAL: 4608 bytes exceeds 4096 budget. 0.1x penalty applied.", "debug"),
      ("Need to cut 512 bytes. Reduce sin from 256 to 192 entries or drop sqrt table.", "observation")]),

    (15, "Merge table-heavy and hybrid approaches",
     [5, 13], "active", None,
     KERNELS_IDEA_15, None, None, ["hybrid", "merge"],
     [("Merging ideas 5 (table-heavy) and 13 (hybrid). Strategy: use best of both.", "observation")]),
]


# ═══════════════════════════════════════════════════════════════════════════
# Shared fixture builder: populates all 15 ideas
# ═══════════════════════════════════════════════════════════════════════════

def _build_full_fixture(fb: FixtureBuilder):
    """Populate a FixtureBuilder with all 15 seed ideas and experiments."""
    for (idea_id, desc, parents, status, conclusion, kernels,
         score, mem_bytes, tags, notes) in SEED_IDEAS:

        fb.add_idea(idea_id, desc, parents, status, conclusion, kernels)

        # Add experiment(s)
        if idea_id == 14:
            # Idea 14: first experiment crashed, second ran but over-budget
            fb.add_experiment(
                idea_id, "Initial run with aggressive tables",
                status="failed",
                error="MemoryError: table allocation exceeded system limits",
                script_content="#!/bin/bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython benchmark/eval_harness.py",
                log_content="Allocating tables...\nsin: 256 entries (2048 bytes)\nexp: 128 entries (1024 bytes)\nlog: 64 entries (512 bytes)\nsqrt: 128 entries (1024 bytes)\nTotal: 4608 bytes\nMemoryError: table pre-allocation check failed",
                err_content="exit code 1\n\nMemoryError: table allocation exceeded system limits",
                tags=tags,
            )
            fb.add_experiment(
                idea_id, "Re-run with budget check disabled (still over budget)",
                status="completed",
                metrics=_metrics(0.08, 4608),
                log_content=_log_json(0.08, 4608),
                tags=tags,
            )
        elif score is not None:
            fb.add_experiment(
                idea_id, f"Evaluate: {desc[:40]}",
                status="completed",
                metrics=_metrics(score, mem_bytes),
                log_content=_log_json(score, mem_bytes),
                tags=tags,
            )
        # Idea 15 has no experiments (active, no runs yet)

        for note_text, note_level in notes:
            fb.add_note(idea_id, note_text, note_level)


# ═══════════════════════════════════════════════════════════════════════════
# T1: Branching — find best idea, branch from it, improve
# ═══════════════════════════════════════════════════════════════════════════

def build_t1_branching(dest: Path):
    """15 ideas forming a DAG. Agent must find best and branch correctly."""
    fb = FixtureBuilder(dest)
    fb.init_project(BASELINE_KERNELS)
    _build_full_fixture(fb)
    fb.finalize()


# ═══════════════════════════════════════════════════════════════════════════
# T2: Experiment Management — iterate on the current approach
# ═══════════════════════════════════════════════════════════════════════════

def build_t2_experiment_mgmt(dest: Path):
    """15 ideas, agent should iterate on the best approach."""
    fb = FixtureBuilder(dest)
    fb.init_project(BASELINE_KERNELS)
    _build_full_fixture(fb)
    fb.finalize()


# ═══════════════════════════════════════════════════════════════════════════
# T3: Error Recovery — investigate and fix failed experiments
# ═══════════════════════════════════════════════════════════════════════════

def build_t3_error_recovery(dest: Path):
    """15 ideas, some with failed experiments. Agent should diagnose and fix."""
    fb = FixtureBuilder(dest)
    fb.init_project(BASELINE_KERNELS)
    _build_full_fixture(fb)
    fb.finalize()


# ═══════════════════════════════════════════════════════════════════════════
# T4: Leaderboard & Search — navigate efficiently, find best direction
# ═══════════════════════════════════════════════════════════════════════════

def build_t4_leaderboard_search(dest: Path):
    """15 ideas across 3 strategies. Agent must navigate and choose."""
    fb = FixtureBuilder(dest)
    fb.init_project(BASELINE_KERNELS)
    _build_full_fixture(fb)
    fb.finalize()


# ═══════════════════════════════════════════════════════════════════════════
# T5: Discovery & Adaptation — agent must discover undocumented endpoints
# ═══════════════════════════════════════════════════════════════════════════

def build_t5_discovery(dest: Path):
    """15 ideas, same as T1-T4. Agent gets stripped PROMPT_api.md and must discover features."""
    fb = FixtureBuilder(dest)
    fb.init_project(BASELINE_KERNELS)
    _build_full_fixture(fb)
    fb.finalize()


# ═══════════════════════════════════════════════════════════════════════════
# T6: Multi-Branch Workflow — agent must work across multiple idea branches
# ═══════════════════════════════════════════════════════════════════════════

def build_t6_multi_branch(dest: Path):
    """15 ideas, but idea 13 and 10 are active. Agent must work on both."""
    fb = FixtureBuilder(dest)
    fb.init_project(BASELINE_KERNELS)

    # Build from SEED_IDEAS but override statuses for ideas 10 and 13
    for (idea_id, desc, parents, status, conclusion, kernels,
         score, mem_bytes, tags, notes) in SEED_IDEAS:

        # Override: idea 10 and 13 become active (not concluded)
        if idea_id == 10:
            status = "active"
            conclusion = None
            notes = list(notes) + [("This idea needs more optimization -- try improving the exp/log kernels", "observation")]
        elif idea_id == 13:
            status = "active"
            conclusion = None
            notes = list(notes) + [("This idea needs more optimization -- try improving the exp/log kernels", "observation")]

        fb.add_idea(idea_id, desc, parents, status, conclusion, kernels)

        # Add experiment(s) — same logic as _build_full_fixture
        if idea_id == 14:
            fb.add_experiment(
                idea_id, "Initial run with aggressive tables",
                status="failed",
                error="MemoryError: table allocation exceeded system limits",
                script_content="#!/bin/bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython benchmark/eval_harness.py",
                log_content="Allocating tables...\nsin: 256 entries (2048 bytes)\nexp: 128 entries (1024 bytes)\nlog: 64 entries (512 bytes)\nsqrt: 128 entries (1024 bytes)\nTotal: 4608 bytes\nMemoryError: table pre-allocation check failed",
                err_content="exit code 1\n\nMemoryError: table allocation exceeded system limits",
                tags=tags,
            )
            fb.add_experiment(
                idea_id, "Re-run with budget check disabled (still over budget)",
                status="completed",
                metrics=_metrics(0.08, 4608),
                log_content=_log_json(0.08, 4608),
                tags=tags,
            )
        elif score is not None:
            fb.add_experiment(
                idea_id, f"Evaluate: {desc[:40]}",
                status="completed",
                metrics=_metrics(score, mem_bytes),
                log_content=_log_json(score, mem_bytes),
                tags=tags,
            )

        for note_text, note_level in notes:
            fb.add_note(idea_id, note_text, note_level)

    fb.finalize()


# ═══════════════════════════════════════════════════════════════════════════
# T7: Analytics & Tag Management — agent must normalize messy tags
# ═══════════════════════════════════════════════════════════════════════════

# Messy tag overrides for T7
_T7_MESSY_TAGS = {
    1: ["table"],
    2: ["Table"],
    3: ["table-v1"],
    4: ["table"],
    5: ["Table"],
    6: ["polynomial"],
    7: ["poly"],
    8: ["Polynomial"],
    9: ["polynomial"],
    10: ["poly"],
    11: ["hybrid"],
    12: ["Hybrid"],
    13: ["hybrid-approach"],
    14: ["over-budget", "failed"],
    15: ["hybrid", "merge"],
}


def build_t7_analytics(dest: Path):
    """15 ideas with multi-experiment data for analytics queries.

    Key fixture properties:
    - Ideas 5, 10, 12, 13 have 3 experiments each (for hierarchical means)
    - Idea 12 is the "fluke": one high score (0.85) + two low repeats (0.30, 0.28)
    - Idea 13 is the "reliable": consistent scores (0.72, 0.74, 0.71)
    - Idea 14 has a failed + over-budget experiment
    - Tags are consistent (not messy — analytics is about querying, not cleanup)
    """
    fb = FixtureBuilder(dest)
    fb.init_project(BASELINE_KERNELS)

    # Multi-experiment score overrides:
    # {idea_id: [(score, mem_bytes, description_suffix), ...]}
    _MULTI_EXPS = {
        5: [
            (0.48, 1280, "run 1: baseline"),
            (0.45, 1280, "run 2: slightly worse"),
            (0.50, 1280, "run 3: minor tuning"),
        ],
        10: [
            (0.55, 0, "run 1: baseline"),
            (0.53, 0, "run 2: variance check"),
            (0.56, 0, "run 3: stable result"),
        ],
        12: [
            (0.85, 384, "run 1: surprisingly high (fluke)"),
            (0.30, 384, "run 2: cannot reproduce"),
            (0.28, 384, "run 3: confirms run 1 was an outlier"),
        ],
        13: [
            (0.72, 768, "run 1: solid result"),
            (0.74, 768, "run 2: consistent"),
            (0.71, 768, "run 3: reliable range"),
        ],
    }

    for (idea_id, desc, parents, status, conclusion, kernels,
         score, mem_bytes, tags, notes) in SEED_IDEAS:

        fb.add_idea(idea_id, desc, parents, status, conclusion, kernels)

        if idea_id in _MULTI_EXPS:
            for exp_score, exp_mem, exp_suffix in _MULTI_EXPS[idea_id]:
                fb.add_experiment(
                    idea_id, f"{desc[:30]}... — {exp_suffix}",
                    status="completed",
                    metrics=_metrics(exp_score, exp_mem),
                    log_content=_log_json(exp_score, exp_mem),
                    tags=tags,
                )
        elif idea_id == 14:
            fb.add_experiment(
                idea_id, "Initial run with aggressive tables",
                status="failed",
                error="MemoryError: table allocation exceeded system limits",
                script_content="#!/bin/bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython benchmark/eval_harness.py",
                log_content="Allocating tables...\nsin: 256 entries (2048 bytes)\nexp: 128 entries (1024 bytes)\nlog: 64 entries (512 bytes)\nsqrt: 128 entries (1024 bytes)\nTotal: 4608 bytes\nMemoryError: table pre-allocation check failed",
                err_content="exit code 1\n\nMemoryError: table allocation exceeded system limits",
                tags=tags,
            )
            fb.add_experiment(
                idea_id, "Re-run with budget check disabled (still over budget)",
                status="completed",
                metrics=_metrics(0.08, 4608),
                log_content=_log_json(0.08, 4608),
                tags=tags,
            )
        elif score is not None:
            fb.add_experiment(
                idea_id, f"Evaluate: {desc[:40]}",
                status="completed",
                metrics=_metrics(score, mem_bytes),
                log_content=_log_json(score, mem_bytes),
                tags=tags,
            )

        for note_text, note_level in notes:
            fb.add_note(idea_id, note_text, note_level)

    fb.finalize()


def build_t8_metadata(dest: Path):
    """15 ideas with messy tags. Agent must understand tag/metric semantics."""
    fb = FixtureBuilder(dest)
    fb.init_project(BASELINE_KERNELS)

    for (idea_id, desc, parents, status, conclusion, kernels,
         score, mem_bytes, _original_tags, notes) in SEED_IDEAS:

        # Use messy tags for T8
        tags = _T7_MESSY_TAGS.get(idea_id, _original_tags)

        fb.add_idea(idea_id, desc, parents, status, conclusion, kernels)

        if idea_id == 14:
            fb.add_experiment(
                idea_id, "Initial run with aggressive tables",
                status="failed",
                error="MemoryError: table allocation exceeded system limits",
                script_content="#!/bin/bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython benchmark/eval_harness.py",
                log_content="Allocating tables...\nsin: 256 entries (2048 bytes)\nexp: 128 entries (1024 bytes)\nlog: 64 entries (512 bytes)\nsqrt: 128 entries (1024 bytes)\nTotal: 4608 bytes\nMemoryError: table pre-allocation check failed",
                err_content="exit code 1\n\nMemoryError: table allocation exceeded system limits",
                tags=tags,
            )
            fb.add_experiment(
                idea_id, "Re-run with budget check disabled (still over budget)",
                status="completed",
                metrics=_metrics(0.08, 4608),
                log_content=_log_json(0.08, 4608),
                tags=tags,
            )
        elif score is not None:
            fb.add_experiment(
                idea_id, f"Evaluate: {desc[:40]}",
                status="completed",
                metrics=_metrics(score, mem_bytes),
                log_content=_log_json(score, mem_bytes),
                tags=tags,
            )

        for note_text, note_level in notes:
            fb.add_note(idea_id, note_text, note_level)

    fb.finalize()


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

BUILDERS = {
    "t1": ("t1_branching/fixture", build_t1_branching),
    "t2": ("t2_experiment_mgmt/fixture", build_t2_experiment_mgmt),
    "t3": ("t3_error_recovery/fixture", build_t3_error_recovery),
    "t4": ("t4_leaderboard_search/fixture", build_t4_leaderboard_search),
    "t5": ("t5_discovery/fixture", build_t5_discovery),
    "t6": ("t6_multi_branch/fixture", build_t6_multi_branch),
    "t7": ("t7_analytics/fixture", build_t7_analytics),
    "t8": ("t8_metadata/fixture", build_t8_metadata),
}


def main():
    targets = sys.argv[1:] or list(BUILDERS.keys())
    for key in targets:
        if key not in BUILDERS:
            print(f"Unknown fixture: {key}. Available: {', '.join(BUILDERS)}", file=sys.stderr)
            sys.exit(1)
        rel_path, builder = BUILDERS[key]
        dest = TESTS_DIR / rel_path
        if dest.exists():
            import stat
            for p in dest.rglob("*"):
                p.chmod(stat.S_IRWXU)
            shutil.rmtree(dest)
        print(f"Building {key} -> {rel_path}/ ...", file=sys.stderr)
        builder(dest)
        print(f"  Done.", file=sys.stderr)

    # Verify: load each fixture and print stats
    print("\n--- Verification ---", file=sys.stderr)
    try:
        # Add parent dirs to path for Store import
        repo_root = TESTS_DIR.parent.parent
        sys.path.insert(0, str(repo_root))
        from the_lab.store import Store

        for key in targets:
            rel_path, _ = BUILDERS[key]
            fixture_path = TESTS_DIR / rel_path
            store = Store(fixture_path)
            ideas = store.list_ideas()
            best_score = 0.0
            for idea in ideas:
                for exp in store.list_experiments(idea["id"]):
                    if exp.get("status") == "completed":
                        s = (exp.get("metrics") or {}).get("score", 0)
                        best_score = max(best_score, s)
            print(f"  {key}: {len(ideas)} ideas, best score = {best_score}", file=sys.stderr)
    except Exception as e:
        print(f"  (verification skipped: {e})", file=sys.stderr)


if __name__ == "__main__":
    main()
