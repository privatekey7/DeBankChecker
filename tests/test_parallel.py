"""Тесты примитивов параллельности (debank_checker/parallel.py)."""
from __future__ import annotations

import threading
import time

import pytest

from debank_checker.parallel import Memo, first_success, run_parallel, unwrap


def test_run_parallel_runs_concurrently_and_keeps_keys():
    barrier = threading.Barrier(3, timeout=2)

    def job(v):
        return lambda: (barrier.wait(), v)[1]

    out = run_parallel({"a": job(1), "b": job(2), "c": job(3)})
    assert out == {"a": 1, "b": 2, "c": 3}


def test_run_parallel_returns_exceptions_as_values():
    out = run_parallel({"ok": lambda: 1, "bad": lambda: 1 / 0})
    assert out["ok"] == 1
    assert isinstance(out["bad"], ZeroDivisionError)
    with pytest.raises(ZeroDivisionError):
        unwrap(out["bad"])


def test_memo_singleflight_calls_once():
    calls = []
    memo = Memo()

    def slow():
        calls.append(1)
        time.sleep(0.1)
        return 42

    out = run_parallel({i: (lambda: memo.get("k", slow)) for i in range(5)})
    assert set(out.values()) == {42}
    assert len(calls) == 1


def test_memo_does_not_cache_errors():
    memo = Memo()
    with pytest.raises(RuntimeError):
        memo.get("k", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert memo.get("k", lambda: 7) == 7


def test_memo_ttl_expires():
    memo = Memo(ttl=0.05)
    assert memo.get("k", lambda: 1) == 1
    assert memo.get("k", lambda: 2) == 1
    time.sleep(0.06)
    assert memo.get("k", lambda: 3) == 3


def test_first_success_hedges_slow_call():
    started = time.perf_counter()
    result = first_success([lambda: (time.sleep(2), "slow")[1], lambda: "fast"], hedge_after=0.05)
    assert result == "fast"
    assert time.perf_counter() - started < 1.0


def test_first_success_moves_on_after_failure():
    assert first_success([lambda: 1 / 0, lambda: "ok"], hedge_after=10) == "ok"


def test_first_success_raises_last_error_when_all_fail():
    with pytest.raises(KeyError):
        first_success([lambda: 1 / 0, lambda: {}["x"]], hedge_after=10)
