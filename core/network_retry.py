"""Bounded exponential backoff shared by backup data transfer channels."""
import random
import time


def retry_wait(attempt, interval):
    from core import cancel
    delay = min(300.0, max(0.0, float(interval)) * (2 ** min(attempt - 1, 12)))
    remaining = random.uniform(delay * 0.5, delay)
    while remaining > 0:
        cancel.check()
        step = min(0.25, remaining)
        time.sleep(step)
        remaining -= step
