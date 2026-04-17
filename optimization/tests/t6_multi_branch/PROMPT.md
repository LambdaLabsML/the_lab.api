# Math Kernel Optimization Under Memory Constraints

## Goal

Two ideas need work: idea 13 (hybrid approach, score 0.75) and idea 10 (polynomial approach, score 0.55). Improve both:
1. Check out idea 13, optimize its kernels, run an experiment
2. Check out idea 10, optimize its kernels, run an experiment
3. Compare the results and note which approach is more promising

## Background

Seven math functions (sin, cos, exp, log, sqrt, sigmoid, tanh) must be implemented in pure Python without the math stdlib. All lookup tables share a 4096-byte memory budget (512 floats at 8 bytes each).

The composite score is a geometric mean of per-kernel scores. Each kernel must achieve 99.99% accuracy to earn speed credit -- below that threshold, accuracy is penalized heavily. If tables exceed the memory budget, the entire score is multiplied by 0.1.

Three strategies have been explored so far:
- **Table-heavy** (ideas 1-5): lookup tables with interpolation
- **Polynomial** (ideas 6-10): Chebyshev/Pade approximations, no tables
- **Hybrid** (ideas 11-15): small tables + polynomial corrections

The current best score is 0.75 from the hybrid approach. There is room to improve.

## Setup

```bash
python benchmark/eval_harness.py
```
