"""
Менеджер прокси: загрузка, ротация, rate limiting
"""

import threading
import time
from collections import deque
from pathlib import Path

from debank_checker.config import DEBUG, PROXIES_FILE, RATE_LIMIT_REQ_PER_MIN


def load_proxies(path: str | Path = PROXIES_FILE) -> list[str]:
    """
    Читает прокси из файла.
    Формат: ip:port:login:password или ip:port
    Возвращает список "http://login:password@ip:port"
    """
    path = Path(path)
    proxies = []
    if not path.exists():
        return proxies
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) == 4:
                ip, port, login, password = parts
                proxies.append(f"http://{login}:{password}@{ip}:{port}")
            elif len(parts) == 2:
                proxies.append(f"http://{parts[0]}:{parts[1]}")
    return proxies


class ProxyManager:
    """
    Ротация прокси с учётом rate limit.
    get_proxy() возвращает прокси с наименьшей нагрузкой.
    report_timeout() временно исключает прокси из выдачи (см. docs/DEBUG_REPORT.md).
    """

    PROXY_COOLDOWN_AFTER_TIMEOUT = 60  # секунд, не выдавать прокси после таймаута

    def __init__(
        self,
        proxies: list[str],
        req_per_min: int = RATE_LIMIT_REQ_PER_MIN,
    ):
        if not proxies:
            self._proxies = []
            self._timestamps: dict[str, deque] = {}
            self._timeout_until: dict[str, float] = {}
            return
        self._proxies = list(proxies)
        self._req_per_min = req_per_min
        self._lock = threading.Lock()
        self._timestamps: dict[str, deque[float]] = {
            p: deque(maxlen=req_per_min * 2) for p in self._proxies
        }
        self._timeout_until: dict[str, float] = {}  # proxy -> time.time() когда снова доступен
        self._idx = 0

    def report_timeout(self, proxy: str | None) -> None:
        """Исключить прокси из выдачи на PROXY_COOLDOWN_AFTER_TIMEOUT сек."""
        if not proxy:
            return
        with self._lock:
            self._timeout_until[proxy] = time.time() + self.PROXY_COOLDOWN_AFTER_TIMEOUT

    def get_proxy(self) -> str | None:
        """Возвращает прокси с наименьшей нагрузкой (round-robin с учётом rate limit)."""
        if not self._proxies:
            return None
        t0 = time.perf_counter()
        with self._lock:
            now = time.time()
            cutoff = now - 60
            best = None
            best_count = float("inf")
            for _ in range(len(self._proxies)):
                p = self._proxies[self._idx % len(self._proxies)]
                self._idx += 1
                if self._timeout_until.get(p, 0) > now:
                    continue
                ts = self._timestamps[p]
                while ts and ts[0] < cutoff:
                    ts.popleft()
                if len(ts) < best_count and len(ts) < self._req_per_min:
                    best = p
                    best_count = len(ts)
                    break
            forced = best is None
            if best is None:
                available = [p for p in self._proxies if self._timeout_until.get(p, 0) <= now]
                best = available[0] if available else min(
                    self._proxies, key=lambda p: self._timeout_until.get(p, 0)
                )
            self._timestamps[best].append(now)
            result = best
        if DEBUG:
            elapsed = time.perf_counter() - t0
            load = len(self._timestamps[result])
            if elapsed > 0.01 or forced or load >= self._req_per_min:
                import sys
                short = result.split("@")[-1] if "@" in result else result
                flags = []
                if forced:
                    flags.append("FORCED")
                if load >= self._req_per_min:
                    flags.append(f"LOAD={load}")
                flag_str = " | " + ", ".join(flags) if flags else ""
                sys.stderr.write(f"[DEBUG] get_proxy | {elapsed:.3f}s | {short}{flag_str}\n")
                sys.stderr.flush()
        return result
