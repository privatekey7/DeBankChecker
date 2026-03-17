"""
Конфигурация DeBank Balance Checker
"""

# Пути к файлам
WALLETS_FILE = "wallets.txt"
PROXIES_FILE = "proxy.txt"
OUTPUT_DIR = "Results"  # папка для Excel-результатов

# API
REQUEST_TIMEOUT = 3  # секунд (быстрый failover при мёртвых прокси, см. docs/DEBUG_REPORT.md)
API_KEY_INIT = "3b92c003-ddc1-4c2d-b36e-781838f362c5"

# Прокси и rate limit
RATE_LIMIT_REQ_PER_MIN = 60  # запросов в минуту на один прокси
RETRY_ATTEMPTS = 10  # попыток при ошибке (с новым прокси, без задержки между попытками)

# Параллелизм
MAX_WORKERS = 500  # максимум одновременных воркеров
PROXY_MULTIPLIER = 5  # max_workers = min(MAX_WORKERS, len(wallets), len(proxies) * PROXY_MULTIPLIER)

# Минимальная сумма для отображения (USD)
MIN_VALUE_DISPLAY = 0.01

# NFT
NFT_POLL_INTERVAL = 3   # сек между попытками при async job
NFT_POLL_MAX_WAIT = 30  # макс время ожидания результата
NFT_REQUEST_TIMEOUT = 3  # таймаут запроса (как REQUEST_TIMEOUT)

# Отладка
DEBUG = False  # True — отладочное логирование (время, попытки, прокси)
