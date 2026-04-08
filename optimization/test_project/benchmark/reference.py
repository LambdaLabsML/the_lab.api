"""Reference implementations — correct answers from math stdlib."""
import math


def ref_sin(x: float) -> float:
    return math.sin(x)

def ref_cos(x: float) -> float:
    return math.cos(x)

def ref_exp(x: float) -> float:
    return math.exp(x)

def ref_log(x: float) -> float:
    return math.log(x) if x > 0 else float("-inf")

def ref_sqrt(x: float) -> float:
    return math.sqrt(x) if x >= 0 else float("nan")

def ref_atan2(y: float, x: float) -> float:
    return math.atan2(y, x)

def ref_sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)

def ref_tanh(x: float) -> float:
    return math.tanh(x)
