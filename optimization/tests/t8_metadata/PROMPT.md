# Math Kernel Optimization Under Memory Constraints

## Goal

The experiment tags are messy and inconsistent. The project also lacks documentation about what tags and metrics mean. Clean this up for future researchers:

1. **Normalize tags**: Rename duplicates (e.g., "Table"→"table", "poly"→"polynomial") so each approach has exactly one tag.
2. **Understand tag purposes**: For each tag, figure out what it represents and which experiments use it. Document this as notes.
3. **Understand metrics**: Determine which metrics should be maximized vs minimized, and what they measure. Document this.
4. **Continue the best approach**: Branch from the most promising idea and run one experiment to improve the score. Tag the new experiment clearly, and document what the tag means for anyone who comes after you.

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
