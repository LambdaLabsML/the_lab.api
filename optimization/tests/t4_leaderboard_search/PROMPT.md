# Math Kernel Optimization Under Memory Constraints

## Goal

Maximize the composite score. Previous researchers explored table-based, polynomial, and hybrid approaches -- find the most promising approach and push it further.

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
