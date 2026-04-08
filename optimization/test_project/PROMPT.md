# Fast Math Kernel Optimization

## Goal

Maximize the **composite score** from `benchmark/eval_harness.py`. The score rewards both accuracy (>= 99% relative accuracy required) and throughput (nanoseconds per call) across 8 math kernels. Higher is better.

## Background

`benchmark/kernels.py` contains naive Python implementations of `sin`, `cos`, `exp`, `log`, `sqrt`, `atan2`, `sigmoid`, and `tanh`. Each can be optimized using polynomial approximations (Chebyshev, minimax), bit manipulation tricks, lookup tables, range reduction, or algebraic identities. No math stdlib or numpy allowed — pure Python arithmetic only.

## Setup

Run the evaluation:
```bash
python benchmark/eval_harness.py
```

Outputs JSON with per-kernel accuracy and throughput, plus a composite score. Modify `benchmark/kernels.py` to improve the score.

## Approach

Each kernel is an independent optimization target. Good strategies:
- **Range reduction** — fold input to a small interval, compute there, unfold
- **Minimax polynomials** — better coefficients than Taylor series for bounded error
- **Bit tricks** — manipulate IEEE 754 floats via `struct.pack`/`struct.unpack`
- **Algebraic identities** — express one function in terms of a faster one (e.g., tanh from sigmoid)
- **Precomputed tables** — trade memory for speed on lookup + interpolation

---

## How Experiments Work

You have access to a local experiment management API. Use it to structure your work.

### Core concepts

- **Idea** — a research direction with its own **git branch** (`idea/<id>`). Status: `active` → `concluded`/`abandoned`.
- **Experiment** — a bash script run under an idea. Produces `metrics` as JSON on the last stdout line.

### Research workflow

The core loop:

1. **Create idea** → `POST /ideas/new {"description": "optimize sin using Chebyshev polynomials"}`
2. **Checkout** → `POST /ideas/<id>/checkout`
3. **Edit** `benchmark/kernels.py` with your optimization
4. **Create experiment** → `POST /ideas/<id>/experiments {"description": "test sin optimization", "script_content": "#!/bin/bash\nset -euo pipefail\npython benchmark/eval_harness.py"}`
5. **Start** → `POST /experiments/<id>/start`
6. **Wait** → `GET /wait?experiment_id=<id>`
7. **Check result** → `GET /experiments/<id>` — look at the `metrics.score` field
8. **Note findings** → `POST /ideas/<id>/note {"text": "sin accuracy improved to 0.9999, score now 1.2", "level": "milestone"}`
9. **Conclude or iterate** → `POST /ideas/<id>/conclude {"conclusion": "Chebyshev gave 10x speedup on sin"}` or create another experiment

### Tips

- Optimize one or two kernels per idea, then conclude and branch to the next
- Check the experiment log if something fails: `GET /experiments/<id>/log?tail=20`
- The eval_harness.py script prints per-kernel results to stderr and JSON to stdout
- You can branch from concluded ideas: `POST /ideas/new {"parent_ids": [<id>], "description": "..."}`

### API reference

All endpoints documented at `GET /openapi.json` or `GET /docs`.
