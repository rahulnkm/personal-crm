"""Counting proxy for Supabase client — used in round-trip regression tests.

Wraps a real (or fake) supabase client and records every terminal .execute()
call (keyed by table + op) and every .rpc() call so tests can assert on counts
without inspecting the real database.
"""

import time


class _BuilderProxy:
    """Wraps a supabase query builder, intercepts .execute() to record the call."""

    def __init__(self, real, table: str, sink: list, latency: float = 0.0):
        # Store directly in __dict__ to avoid triggering __getattr__
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_table", table)
        object.__setattr__(self, "_sink", sink)
        object.__setattr__(self, "_latency", latency)
        object.__setattr__(self, "_op", "select")

    def __getattr__(self, name: str):
        real = object.__getattribute__(self, "_real")
        attr = getattr(real, name)

        if not callable(attr):
            return attr

        def _wrapper(*args, **kwargs):
            # Track the operation type for mutation methods
            if name in ("update", "insert", "upsert", "delete"):
                object.__setattr__(self, "_op", name)

            result = attr(*args, **kwargs)

            if name == "execute":
                latency = object.__getattribute__(self, "_latency")
                if latency:
                    time.sleep(latency)
                table = object.__getattribute__(self, "_table")
                op = object.__getattribute__(self, "_op")
                sink = object.__getattribute__(self, "_sink")
                sink.append((table, op))
                return result
            else:
                # Reassign the real builder (some builders return a new instance
                # after each filter call), keep chaining via self
                object.__setattr__(self, "_real", result)
                return self

        return _wrapper


class CountingClient:
    """Proxy around a supabase client that counts terminal execute() / rpc() calls.

    Args:
        real: A supabase client (or any object with .table() / .rpc() methods).
        latency: Optional seconds to sleep before each .execute() — for benchmarks.

    Usage::

        spy = CountingClient(supabase)
        spy.table("contacts").select("*").execute()
        assert spy.count("contacts", "select") == 1
        assert spy.total() == 1
    """

    def __init__(self, real, latency: float = 0.0):
        self._real = real
        self._latency = latency
        self.calls: list[tuple[str, str]] = []

    def table(self, name: str) -> _BuilderProxy:
        return _BuilderProxy(self._real.table(name), name, self.calls, self._latency)

    def rpc(self, name: str, params=None):
        self.calls.append(("rpc", name))
        if self._latency:
            time.sleep(self._latency)
        return self._real.rpc(name, params or {})

    def count(self, table: str, op: str) -> int:
        """Return how many times (table, op) was executed."""
        return sum(1 for t, o in self.calls if t == table and o == op)

    def rpc_count(self, name: str) -> int:
        """Return how many times rpc(name) was called."""
        return sum(1 for t, o in self.calls if t == "rpc" and o == name)

    def total(self) -> int:
        """Total number of execute() + rpc() calls recorded."""
        return len(self.calls)
