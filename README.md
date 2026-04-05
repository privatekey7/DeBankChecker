# DeBank Balance Checker

[![CI](https://github.com/privatekey7/DeBankChecker/actions/workflows/ci.yml/badge.svg)](https://github.com/privatekey7/DeBankChecker/actions/workflows/ci.yml)

CLI‑утилита на Python для массовой проверки кошельков через DeBank API (**Tokens + DeFi + NFT**) с экспортом результатов (**Excel / CSV / JSON**).

**TG:** `https://t.me/privatekey_ai`

---

## Возможности

- **Массовая проверка** списка адресов из `wallets.txt`
- **Прокси‑ротация** + rate limiting / cooldown (для стабильности)
- **Параллельная обработка** кошельков
- **Экспорт**:
  - Excel (`.xlsx`)
  - CSV
  - JSON
- **Фильтры экспорта** по сетям/токенам/NFT/протоколам (в меню после проверки)

---

## Требования

- **Python 3.10+** (рекомендуется)
- Windows / macOS / Linux

Зависимости перечислены в `requirements.txt` (ключевые: `curl_cffi`, `openpyxl`, `questionary`, `colorama`).

---

## Установка

В папке проекта:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Дальше:

```bash
pip install -r requirements.txt
```

---

## Подготовка файлов

### `wallets.txt`

- **Один адрес на строку**
- Пустые строки игнорируются
- Строки, начинающиеся с `#`, считаются комментариями

Пример:

```text
# my wallets
0x0000000000000000000000000000000000000000
0x1111111111111111111111111111111111111111
```

### `proxy.txt` (обязательно)

Прокси обязательны: при пустом `proxy.txt` программа завершится с ошибкой.

Поддерживаемые форматы строк:

- `ip:port`
- `ip:port:login:password`

Пример:

```text
127.0.0.1:8080
10.10.10.10:3128:user:pass
```

---

## Запуск

```bash
python main.py
```

Поток работы:

1. Запускается проверка всех кошельков
2. Печатается прогресс‑бар
3. После завершения открывается **интерактивное меню** экспорта (что именно выгружать)
4. Вы выбираете формат (**Excel / CSV / JSON**)
5. Файлы сохраняются в папку `Results/`

---

## Результаты

Вывод сохраняется в:

- `Results/`

Файлы именуются с учётом выбранного экспорта и времени запуска.

---
