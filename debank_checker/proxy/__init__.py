"""Прокси: загрузка, ротация, rate limiting."""

from debank_checker.proxy.manager import ProxyManager, load_proxies

__all__ = ["ProxyManager", "load_proxies"]
