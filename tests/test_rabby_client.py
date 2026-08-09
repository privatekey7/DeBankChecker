"""Тесты клиента Rabby API.

Главный тест — воспроизведение подписи из реальной HAR-записи веб-версии Rabby
байт-в-байт. Это не тавтология: ожидаемое значение (x-api-sign) снято с боевого
клиента, а не вычислено нашей же формулой. Совпадение доказывает, что схема
подписи верна.
"""

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest

from debank_checker.api.rabby_client import generate_nonce, sign_request, sort_params

FIXTURE = Path(__file__).parent / "fixtures" / "rabby_signed_requests.har"


def _header(entry: dict, name: str) -> str | None:
    for h in entry["request"]["headers"]:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def _load_signed_requests() -> list[dict]:
    har = json.loads(FIXTURE.read_text())
    return har["log"]["entries"]


@pytest.mark.parametrize("entry", _load_signed_requests())
def test_sign_request_reproduces_har_signature(entry: dict) -> None:
    """Подпись, вычисленная из nonce/ts запроса, совпадает с x-api-sign из HAR."""
    req = entry["request"]
    parsed = urlparse(req["url"])
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    nonce = _header(entry, "x-api-nonce")
    ts = int(_header(entry, "x-api-ts"))
    expected = _header(entry, "x-api-sign")

    result = sign_request(params, req["method"], parsed.path, nonce=nonce, ts=ts)

    assert result["signature"] == expected


def test_sort_params_orders_by_key() -> None:
    assert sort_params({"b": "2", "a": "1", "c": "3"}) == "a=1&b=2&c=3"


def test_sort_params_empty() -> None:
    assert sort_params({}) == ""


def test_generate_nonce_format() -> None:
    nonce = generate_nonce()
    assert nonce.startswith("n_")
    assert len(nonce) == 42  # "n_" + 40 символов


def test_generate_nonce_is_random() -> None:
    assert generate_nonce() != generate_nonce()


def test_sign_request_is_deterministic_for_fixed_nonce_ts() -> None:
    a = sign_request({"id": "0xabc"}, "GET", "/v1/user/total_balance", nonce="n_x", ts=100)
    b = sign_request({"id": "0xabc"}, "GET", "/v1/user/total_balance", nonce="n_x", ts=100)
    assert a["signature"] == b["signature"]


def test_rabby_client_requires_proxy() -> None:
    from debank_checker.api.rabby_client import RabbyClient

    with pytest.raises(ValueError):
        RabbyClient(proxy="")
