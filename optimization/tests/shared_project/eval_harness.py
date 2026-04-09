#!/usr/bin/env python3
"""Evaluate the solver's guess against a hidden target."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from solver import guess

TARGET = 42.0

value = guess()
error = abs(value - TARGET) / TARGET
score = max(0.0, 1.0 - error)

output = {
    "metrics": {
        "score": round(score, 6),
        "guess": value,
        "error": round(error, 6),
    }
}

# Human-readable to stderr
print(f"guess={value}, target={TARGET}, error={error:.4f}, score={score:.4f}", file=sys.stderr)

# Lab-compatible JSON on stdout
print(json.dumps(output))
