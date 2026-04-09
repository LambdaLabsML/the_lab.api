# Fast Math Kernel Optimization Under Memory Constraints

## Goal

Maximize the **composite score** from `benchmark/eval_harness.py`. The score rewards both accuracy (>= 99.99% relative accuracy required) and throughput (nanoseconds per call) across 6 math kernels, subject to a **shared 4KB memory budget** for lookup tables.

## Background

`benchmark/kernels.py` contains 6 correct-but-slow math functions: `sin`, `cos`, `exp`, `log`, `sqrt`, `sigmoid`, and `tanh`. They use high-degree polynomial approximations that achieve 99.99% accuracy but are slow.

The optimization challenge: make them faster while maintaining accuracy, using a **shared pool of 512 floats (4KB)** for lookup tables. The key tradeoff is allocating this memory between functions — giving `sin` a large table means less room for `exp`.

### Dependencies

Functions share code:
- `fast_sigmoid` calls `fast_exp` — improving exp also speeds up sigmoid
- `fast_tanh` calls `fast_sigmoid` → `fast_exp` — a chain of dependencies

Optimizing `exp` gives you a 3-for-1 improvement.

### Memory Budget

The `TABLES` dict in `kernels.py` holds pre-computed lookup tables. Each float = 8 bytes. Total across all tables must be ≤ 4096 bytes (512 floats). Going over budget applies a 10× score penalty.

### Scoring

Per-kernel score:
- Accuracy < 99.99%: `score = accuracy × 0.1` (heavy penalty)
- Accuracy ≥ 99.99%: `score = accuracy × (1000 / ns_per_call)` (rewards speed)

Composite = geometric mean across all kernels × memory penalty.

## Setup

```bash
python benchmark/eval_harness.py
```

Each evaluation runs 2M test points per kernel with multiple benchmark passes — takes **60-120s** with naive implementations (faster as you optimize). Use `GET /wait?experiment_id=<id>` to block until it finishes rather than polling.

## Strategies

- **Table + interpolation**: pre-compute values at regular intervals, interpolate between them. Uses memory but very fast.
- **Minimax polynomials**: better coefficients than Taylor series for bounded error on a fixed interval. No memory needed.
- **Hybrid**: use a small table for range reduction, then a short polynomial for the remainder.
- **Bit tricks**: manipulate IEEE 754 floats via `struct.pack`/`struct.unpack` for fast initial guesses.
- **Exploit dependencies**: invest memory in `exp` since sigmoid and tanh benefit too.
- **Range reduction**: fold inputs to small intervals to minimize table size needed.

The starting implementations are correct and meet accuracy targets, but slow (many polynomial terms). The job is speed, not correctness.
