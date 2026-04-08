"""Fast math kernels — correct but slow implementations to be optimized.

Constraints:
  - MEMORY_BUDGET: total bytes for all lookup tables combined (4096 bytes)
  - No math stdlib (math.sin, math.exp, etc.)
  - No numpy/scipy
  - Pure Python arithmetic + struct for bit tricks
  - Must handle the full input range specified in eval_harness.py
  - Accuracy target: 99.99% relative accuracy (max_rel_error < 0.0001)

Optimization levers:
  - Allocate TABLES memory between functions (tradeoff: more table = faster but less for others)
  - Choose polynomial degree vs table+interpolation vs hybrid
  - Exploit function dependencies: sigmoid uses exp, tanh uses sigmoid
  - Range reduction to minimize table size needed
  - Bit tricks via struct.pack/unpack for initial guesses

The TABLES dict below is pre-allocated at import time. Each value is a list
of floats. Total memory = sum(len(t) * 8 for t in TABLES.values()) must be
<= MEMORY_BUDGET. The eval harness enforces this.
"""
import struct

# Shared memory budget for ALL lookup tables (in bytes, 8 bytes per float)
MEMORY_BUDGET = 4096  # = 512 floats total across all tables

# Pre-allocated lookup tables — agent distributes budget here
# Total floats across all tables must be <= MEMORY_BUDGET // 8 = 512
TABLES: dict[str, list[float]] = {
    # Example: "sin": [precomputed values...],
    # Start empty — agent allocates as needed
}


def fast_sin(x: float) -> float:
    """Compute sin(x) for x in [-2pi, 2pi]. Target: 99.99% accuracy."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586

    # Range reduce to [-pi, pi]
    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI

    # Polynomial approximation (many terms for accuracy — slow)
    x2 = x * x
    # Taylor series with 9 terms
    result = x
    term = x
    for n in range(1, 9):
        term *= -x2 / ((2 * n) * (2 * n + 1))
        result += term
    return result


def fast_cos(x: float) -> float:
    """Compute cos(x) for x in [-2pi, 2pi]. Target: 99.99% accuracy."""
    PI = 3.141592653589793
    TWO_PI = 6.283185307179586

    x = x % TWO_PI
    if x > PI:
        x -= TWO_PI
    if x < -PI:
        x += TWO_PI

    x2 = x * x
    result = 1.0
    term = 1.0
    for n in range(1, 9):
        term *= -x2 / ((2 * n - 1) * (2 * n))
        result += term
    return result


def fast_exp(x: float) -> float:
    """Compute e^x for x in [-10, 10]. Target: 99.99% accuracy."""
    if x > 10:
        x = 10.0
    if x < -10:
        x = -10.0

    # Range reduction: e^x = 2^k * e^r where |r| <= ln2/2
    LN2 = 0.6931471805599453
    k = int(x / LN2 + (0.5 if x >= 0 else -0.5))
    r = x - k * LN2

    # Taylor series for e^r (slow — many terms needed)
    result = 1.0
    term = 1.0
    for n in range(1, 14):
        term *= r / n
        result += term

    # Multiply by 2^k
    if k >= 0:
        for _ in range(k):
            result *= 2.0
    else:
        for _ in range(-k):
            result *= 0.5

    return result


def fast_log(x: float) -> float:
    """Compute ln(x) for x in (0, 1000]. Target: 99.99% accuracy."""
    if x <= 0:
        return float("-inf")

    # Reduce to [1, 2): x = m * 2^e
    e = 0
    m = x
    while m >= 2.0:
        m *= 0.5
        e += 1
    while m < 1.0:
        m *= 2.0
        e -= 1

    # ln(m) via atanh series (faster convergence than naive Taylor)
    LN2 = 0.6931471805599453
    y = (m - 1.0) / (m + 1.0)
    y2 = y * y
    result = y
    yn = y
    for k in range(1, 15):
        yn *= y2
        result += yn / (2 * k + 1)

    return 2.0 * result + e * LN2


def fast_sqrt(x: float) -> float:
    """Compute sqrt(x) for x in [0, 1e6]. Target: 99.99% accuracy."""
    if x < 0:
        return float("nan")
    if x == 0:
        return 0.0

    # Initial guess via bit manipulation
    try:
        bits = struct.unpack('Q', struct.pack('d', x))[0]
        bits = (bits >> 1) + (0x1FF8000000000000 >> 1)
        guess = struct.unpack('d', struct.pack('Q', bits))[0]
    except Exception:
        guess = x * 0.5 if x >= 1 else x * 2

    # Newton iterations (many for 99.99% accuracy)
    for _ in range(8):
        guess = 0.5 * (guess + x / guess)
    return guess


def fast_sigmoid(x: float) -> float:
    """Compute 1/(1+e^-x) for x in [-20, 20]. Target: 99.99% accuracy."""
    # Depends on fast_exp — improving exp improves sigmoid too
    if x >= 15:
        return 1.0
    if x <= -15:
        return 0.0
    if x >= 0:
        return 1.0 / (1.0 + fast_exp(-x))
    ex = fast_exp(x)
    return ex / (1.0 + ex)


def fast_tanh(x: float) -> float:
    """Compute tanh(x) for x in [-10, 10]. Target: 99.99% accuracy."""
    # Depends on fast_sigmoid -> fast_exp chain
    if x > 8:
        return 1.0
    if x < -8:
        return -1.0
    return 2.0 * fast_sigmoid(2.0 * x) - 1.0
