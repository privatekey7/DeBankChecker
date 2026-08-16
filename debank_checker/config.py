"""
Конфигурация DeBank Balance Checker
"""

# Пути к файлам
WALLETS_FILE = "wallets.txt"
PROXIES_FILE = "proxy.txt"
OUTPUT_DIR = "Results"  # папка для Excel-результатов

# API
REQUEST_TIMEOUT = 3  # секунд (быстрый failover при мёртвых прокси, см. docs/DEBUG_REPORT.md)

# --- Rabby API -----------------------------------------------------------
# Init-ключ ротируется сервером через x-set-api-key; ротированный ключ хранится
# в общем магазине api/rabby_client.py — новый клиент на попытку продолжает
# с последнего выданного, а не стартует заново с init-ключа.
RABBY_API_KEY_INIT = "7cee6f31-6611-4821-beb8-6ca9e29ed965"
# Время выдачи init-ключа (из HAR веб-клиента). Отправляется как x-api-time:
# сервер ожидает время ВЫДАЧИ ключа, а не время запроса — вместе с кейсингом
# заголовков и отсутствием x-api-ver это сверено с HAR (иначе фейковый 429).
RABBY_API_KEY_INIT_TIME = 1762656362
# x-version = версия клиента Rabby (из HAR расширения), под которую записана
# схема; при поломке подписи — обнови из свежего HAR.
RABBY_CLIENT_VERSION = "0.94.2"
# is_core=true → в total и в токенах только проверенные (core) токены, скам отсекается.
RABBY_IS_CORE = True

# Прокси и rate limit
RATE_LIMIT_REQ_PER_MIN = 60  # запросов в минуту на один прокси
RETRY_ATTEMPTS = 10  # попыток при ошибке (с новым прокси, без задержки между попытками)

# Параллелизм
MAX_WORKERS = 500  # максимум одновременных воркеров
PROXY_MULTIPLIER = 5  # max_workers = min(MAX_WORKERS, len(wallets), len(proxies) * PROXY_MULTIPLIER)

# Минимальная сумма для отображения (USD)
MIN_VALUE_DISPLAY = 0.01

# --- Защита от «фантомных» балансов -------------------------------------
# Баланс принимается, только если подтверждён CORROBORATION_MIN_AGREE
# независимыми выборками, сошедшимися в пределах допуска. Для Rabby итог
# авторитетен (total_usd_value из /v1/user/total_balance), поэтому по
# умолчанию MIN_AGREE=1 — корроборация не тратит лишние запросы, но механика
# остаётся как страховка.
CORROBORATION_ENABLED = True      # False — принять первую же выборку
CORROBORATION_MIN_AGREE = 1       # сошедшихся выборок нужно для приёма
CORROBORATION_MAX_FETCHES = 8     # бюджет успешных выборок на кошелёк
CORROBORATION_REL_TOL = 0.02      # относительный допуск согласия (2%)
CORROBORATION_ABS_TOL = 1.0       # абсолютный допуск согласия (USD)

# NFT
NFT_POLL_INTERVAL = 3   # сек между попытками при async job
NFT_POLL_MAX_WAIT = 30  # макс время ожидания результата
NFT_REQUEST_TIMEOUT = 3  # таймаут запроса (как REQUEST_TIMEOUT)

# Отладка
DEBUG = False  # True — отладочное логирование (время, попытки, прокси)
