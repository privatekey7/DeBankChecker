"""
Менеджер прокси: загрузка, ротация, rate limiting
"""
from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

from debank_checker.config import DEBUG, PROXIES_FILE, PROXY_COOLDOWN_429_SEC, RATE_LIMIT_REQ_PER_MIN


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


def drop_dead_proxies(proxies: list[str], timeout: float = 3.0) -> tuple[list[str], list[str]]:
    """Параллельная предпроверка: прокси, отвергающие подключение (407 /
    CONNECT failed), отбрасываются. Любой HTTP-ответ — прокси жив; таймаут —
    тоже оставляем (мог просто притормозить). Заодно прогревает keep-alive
    соединения к api.rabby.io. Возвращает (живые, мёртвые)."""
    from concurrent.futures import ThreadPoolExecutor

    from debank_checker.api import http

    def alive(proxy: str) -> bool:
        try:
            http.get("https://api.rabby.io/", proxy, timeout=timeout)
        except http.ProxyDead:
            return False
        except Exception:  # noqa: BLE001
            pass
        return True

    if not proxies:
        return [], []
    with ThreadPoolExecutor(max_workers=min(len(proxies), 128)) as ex:
        flags = list(ex.map(alive, proxies))
    return ([p for p, ok in zip(proxies, flags) if ok],
            [p for p, ok in zip(proxies, flags) if not ok])


class ProxyManager:
    """
    Ротация прокси с учётом rate limit.
    get_proxy() возвращает прокси с наименьшей нагрузкой.
    report_timeout() / report_rate_limited() временно исключают прокси из выдачи.
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
        self._cooldown(proxy, self.PROXY_COOLDOWN_AFTER_TIMEOUT)

    def report_rate_limited(self, proxy: str | None) -> None:
        """Прокси получил 429/403 — не выдавать PROXY_COOLDOWN_429_SEC сек."""
        self._cooldown(proxy, PROXY_COOLDOWN_429_SEC)

    def report_dead(self, proxy: str | None) -> None:
        """Прокси отверг подключение (407) — исключить до конца запуска."""
        self._cooldown(proxy, float("inf"))

    def _cooldown(self, proxy: str | None, seconds: float) -> None:
        if not proxy or not self._proxies:
            return
        with self._lock:
            until = time.time() + seconds
            self._timeout_until[proxy] = max(self._timeout_until.get(proxy, 0), until)

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
