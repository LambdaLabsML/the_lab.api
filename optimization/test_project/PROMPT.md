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

---

## How Experiments Work

You have access to a local experiment management API. Use it to structure your work.

### Core concepts

- **Idea** — a research direction with its own **git branch** (`idea/<id>`). Status: `active` → `concluded`/`abandoned`.
- **Experiment** — a bash script run under an idea. Produces `metrics` as JSON on the last stdout line.

### Research workflow

1. **Create idea** → `POST /ideas/new {"description": "allocate 256 floats to exp table, use interpolation"}`
2. **Checkout** → `POST /ideas/<id>/checkout`
3. **Edit** `benchmark/kernels.py` — modify TABLES and/or function implementations
4. **Create experiment** → `POST /ideas/<id>/experiments {"description": "test exp table allocation", "script_content": "#!/bin/bash\nset -euo pipefail\npython benchmark/eval_harness.py "}`
5. **Start** → `POST /experiments/<id>/start`
6. **Wait** → `GET /wait?experiment_id=<id>`
7. **Check result** → `GET /experiments/<id>` — look at `metrics.score`, `metrics.memory_used_bytes`
8. **Note findings** → `POST /ideas/<id>/note {"text": "exp table gave 3x speedup, score 1.8 → 3.2", "level": "milestone"}`
9. **Conclude or iterate** → branch into next idea for different allocation strategy

### Tips

- Start by profiling: which kernels are slowest? Focus memory there.
- The dependency chain (exp→sigmoid→tanh) means optimizing exp has outsized impact.
- Try different memory splits: 50% to exp, 25% to sin/cos, 25% to log.
- Branch from concluded ideas to try variations on promising allocations.
- Check `memory_used_bytes` — going over budget kills your score.

### API reference

`GET /openapi.json` or `GET /docs`
