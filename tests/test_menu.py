"""Меню экспорта: Ctrl+C прерывает работу, а не запускает экспорт по умолчанию."""
from __future__ import annotations

import pytest

from debank_checker.ui import menu


class _Interrupted:
    def unsafe_ask(self):
        raise KeyboardInterrupt


@pytest.mark.parametrize("call", [lambda: menu.show_menu([]), menu.ask_format, menu.ask_continue])
def test_ctrl_c_in_menu_is_not_swallowed(monkeypatch, call):
    monkeypatch.setattr(menu.questionary, "select", lambda *a, **k: _Interrupted())
    with pytest.raises(KeyboardInterrupt):
        call()
