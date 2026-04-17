# Math Kernel Optimization Under Memory Constraints

## Goal

Systematically explore 4 different optimization approaches for the math kernels. For each approach:
1. Create a new idea branching from a promising parent
2. Edit benchmark/kernels.py with your approach
3. Run an experiment and wait for results
4. Check /orient or /backlog before starting the next idea — the user may submit suggestions

**Important:** Between ideas, always check for suggestions via /orient or /backlog. If a **high-priority** suggestion appears, adopt and work on it immediately — before continuing your planned ideas. Low-priority suggestions can wait until you finish your current batch of work.

## Background

Seven math functions (sin, cos, exp, log, sqrt, sigmoid, tanh) must be implemented in pure Python without the math stdlib. All lookup tables share a 4096-byte memory budget (512 floats at 8 bytes each).

The composite score is a geometric mean of per-kernel scores. Each kernel must achieve 99.99% accuracy to earn speed credit -- below that threshold, accuracy is penalized heavily. If tables exceed the memory budget, the entire score is multiplied by 0.1.

Three strategies have been explored so far:
- **Table-heavy** (ideas 1-5): lookup tables with interpolation
- **Polynomial** (ideas 6-10): Chebyshev/Padé approximations, no tables
- **Hybrid** (ideas 11-15): small tables + polynomial corrections

The current best score is 0.75 from the hybrid approach. There is room to improve.

## Setup

```bash
python benchmark/eval_harness.py
```
