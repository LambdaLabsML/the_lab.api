"""Cheap event-loop responsiveness measurements; no filesystem work."""
import asyncio
from collections import deque

_samples = deque(maxlen=300)


async def monitor_loop():
    loop = asyncio.get_running_loop()
    while True:
        target = loop.time() + 1.0
        await asyncio.sleep(1.0)
        _samples.append(max(0.0, (loop.time() - target) * 1000))


def snapshot():
    values = sorted(_samples)
    return {
        "samples": len(values),
        "p95_ms": round(values[min(len(values) - 1, int(len(values) * .95))], 2) if values else 0,
        "max_ms": round(max(values, default=0), 2),
    }
