"""Fast math kernels — naive implementations to be optimized.

Each function has a correct but slow implementation. The agent's job is to
replace these with faster versions (bit tricks, polynomial approximations,
lookup tables, etc.) while maintaining accuracy.

Rules:
  - No calls to math stdlib (math.sin, math.exp, etc.)
  - No numpy/scipy
  - Pure Python arithmetic + bit operations only
  - Must handle the full input range specified in eval_harness.py
"""
import struct


def fast_sin(x: float) -> float:
    """Compute sin(x) for x in [-2π, 2π]."""
    # Naive: Taylor series (5 terms)
    PI = 3.141592653589793
    # Reduce to [-π, π]
    x = x % (2 * PI)
    if x > PI:
        x -= 2 * PI
    x2 = x * x
    return x - x2 * x / 6 + x2 * x2 * x / 120 - x2 * x2 * x2 * x / 5040


def fast_cos(x: float) -> float:
    """Compute cos(x) for x in [-2π, 2π]."""
    PI = 3.141592653589793
    x = x % (2 * PI)
    if x > PI:
        x -= 2 * PI
    x2 = x * x
    return 1 - x2 / 2 + x2 * x2 / 24 - x2 * x2 * x2 / 720


def fast_exp(x: float) -> float:
    """Compute e^x for x in [-10, 10]."""
    # Naive: Taylor series (12 terms)
    result = 1.0
    term = 1.0
    for i in range(1, 13):
        term *= x / i
        result += term
    return result


def fast_log(x: float) -> float:
    """Compute ln(x) for x in (0, 1000]."""
    if x <= 0:
        return float("-inf")
    # Naive: reduce to [1, 2) then series
    exp = 0
    v = x
    while v >= 2.0:
        v /= 2.0
        exp += 1
    while v < 1.0:
        v *= 2.0
        exp -= 1
    # v in [1, 2), compute ln(v) via series around 1
    y = (v - 1) / (v + 1)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 10):
        yn *= y2
        result += yn / (2 * k + 1)
    return 2 * result + exp * 0.6931471805599453


def fast_sqrt(x: float) -> float:
    """Compute sqrt(x) for x in [0, 1e6]."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0
    # Newton's method (6 iterations from rough initial guess)
    guess = x * 0.5 if x >= 1 else x * 2
    for _ in range(6):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_atan2(y: float, x: float) -> float:
    """Compute atan2(y, x) for any real y, x (not both zero)."""
    PI = 3.141592653589793
    if x == 0 and y == 0:
        return 0.0
    # Reduce to atan(z) where |z| <= 1
    if x == 0:
        return PI / 2 if y > 0 else -PI / 2
    z = y / x
    if abs(z) > 1:
        # atan(z) = sign(z)*π/2 - atan(1/z)
        a = _atan_unit(1.0 / z)
        a = (PI / 2 - a) if z > 0 else (-PI / 2 - a)
    else:
        a = _atan_unit(z)
    if x < 0:
        a += PI if y >= 0 else -PI
    return a


def _atan_unit(z: float) -> float:
    """Approximate atan(z) for |z| <= 1 via polynomial."""
    # 5-term Taylor: atan(z) ≈ z - z³/3 + z⁵/5 - z⁷/7 + z⁹/9
    z2 = z * z
    return z * (1 - z2 * (1/3 - z2 * (1/5 - z2 * (1/7 - z2 / 9))))


def fast_sigmoid(x: float) -> float:
    """Compute 1/(1+e^-x) for x in [-20, 20]."""
    # Naive: use our fast_exp
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Compute tanh(x) for x in [-10, 10]."""
    # tanh(x) = 2*sigmoid(2x) - 1
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
