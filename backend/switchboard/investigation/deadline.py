"""Bound a queued investigation while preserving unrestricted CLI execution."""

import time
from contextlib import contextmanager
from contextvars import ContextVar

_deadline: ContextVar[float | None] = ContextVar("investigation_deadline", default=None)


@contextmanager
def investigation_deadline(deadline: float):
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def require_time(seconds: float = 125):
    # One provider call allows 60 seconds plus one bounded retry and backoff.
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() + seconds > deadline:
        raise TimeoutError("Investigation deadline reached; awaiting SQS retry")
