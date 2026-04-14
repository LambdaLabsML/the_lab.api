# Math Kernel Optimization Under Memory Constraints

## Goal

Analyze the experimental data across all 15 ideas and answer these questions. Document each answer as a note on the most relevant idea.

### Questions

1. **Per-approach mean score**: What is the mean composite score for each approach (table-heavy, polynomial, hybrid)? For approaches with multiple experiments per idea, average within each idea first, then average across ideas.

2. **Memory vs score**: Among completed experiments, which approach achieves the best score-to-memory ratio? (i.e., score per byte of memory used)

3. **Best exp-kernel accuracy**: Which specific idea achieved the highest accuracy for the `exp` kernel?

4. **Over-budget experiments**: List all experiments that used more than 4000 bytes of memory.

5. **Most promising idea to continue**: Which single idea is the most promising to continue working on? Consider not just peak scores but also consistency across repeated experiments — a reliable 0.70 is more promising than a one-time 0.85 that doesn't reproduce.

6. **Metric interpretation**: The experiments track a metric called `convergence_gap`. Should this be minimized or maximized? How does the leaderboard rank experiments by this metric?

After answering, continue from the most promising idea and run one more experiment to improve the score.

## Background

Seven math functions (sin, cos, exp, log, sqrt, sigmoid, tanh) must be implemented in pure Python without the math stdlib. All lookup tables share a 4096-byte memory budget (512 floats at 8 bytes each).

The composite score is a geometric mean of per-kernel scores. Each kernel must achieve 99.99% accuracy to earn speed credit -- below that threshold, accuracy is penalized heavily. If tables exceed the memory budget, the entire score is multiplied by 0.1.

Three strategies have been explored so far:
- **Table-heavy** (ideas 1-5): lookup tables with interpolation
- **Polynomial** (ideas 6-10): Chebyshev/Padé approximations, no tables
- **Hybrid** (ideas 11-15): small tables + polynomial corrections

## Setup

```bash
python benchmark/eval_harness.py
```
