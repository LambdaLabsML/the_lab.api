# The Lab: Autoresearch Optimization

This optimization target runs The Lab over `karpathy/autoresearch`.

The active project at `optimization/proj` is a clone of:

```text
https://github.com/karpathy/autoresearch
```

The optimization surface is `train.py`. The metric is `val_bpb` / `loss`, and lower is better.

## Quick Start

```bash
./optimization/lab-optimize.sh reset
./optimization/lab-optimize.sh start 9000
./optimization/lab-optimize.sh baseline
./optimization/lab-optimize.sh agent sonnet
```

The upstream data preparation still applies:

```bash
cd optimization/proj
uv sync
uv run prepare.py
```

`prepare.py` downloads/prepares data and must not be modified by optimization agents.

## What Runs

Experiments should use:

```bash
python .the_lab/artifacts/run_eval.py
```

The wrapper runs:

```bash
uv run train.py
```

It parses the final summary from `train.py`, especially:

```text
val_bpb:          0.997900
peak_vram_mb:     45060.2
```

and emits Lab-compatible JSON metrics:

- `val_bpb`: primary metric, minimize
- `loss`: alias of `val_bpb`, minimize
- `peak_vram_mb` / `peak_vram_gb`: secondary resource metric
- `mfu_percent`, `num_steps`, `num_params_M`, `depth`: diagnostics

Metric directions are configured in `optimization/proj/.the_lab/metric_directions.json`.

## Archived Target

The previous Lab API self-optimization target has been moved to:

```text
optimization/archive_labs/lab_api_self_optimization/
```
