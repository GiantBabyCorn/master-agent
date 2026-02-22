from __future__ import annotations

import time
from dataclasses import dataclass
from functools import wraps
from threading import Lock
from typing import Callable, TypeVar


T = TypeVar("T")


@dataclass
class CircuitBreakerState:
    failure_count: int = 0
    open_until: float = 0.0


class CircuitBreaker:
    def __init__(self, failure_threshold: int, recovery_seconds: int) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._state = CircuitBreakerState()
        self._lock = Lock()

    def _is_open(self) -> bool:
        return time.time() < self._state.open_until

    def before_call(self) -> None:
        if self._is_open():
            raise RuntimeError("Circuit breaker is open")

    def on_success(self) -> None:
        with self._lock:
            self._state.failure_count = 0
            self._state.open_until = 0.0

    def on_failure(self) -> None:
        with self._lock:
            self._state.failure_count += 1
            if self._state.failure_count >= self.failure_threshold:
                self._state.open_until = time.time() + self.recovery_seconds


def with_retry(max_attempts: int, base_delay_ms: int) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except Exception:  # noqa: BLE001
                    if attempt >= max_attempts:
                        raise
                    time.sleep((base_delay_ms * (2 ** (attempt - 1))) / 1000.0)

        return wrapper

    return decorator
