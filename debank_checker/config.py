"""
Конфигурация DeBank Balance Checker
"""
from __future__ import annotations

import os

# Пути к файлам
WALLETS_FILE = "wallets.txt"
PROXIES_FILE = "proxy.txt"
OUTPUT_DIR = "Results"  # папка для результатов экспорта

# API
REQUEST_TIMEOUT = 8  # секунд на запрос к Rabby (3 с было мало для complex_app_list)

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
RETRY_ATTEMPTS = 10  # минимум попыток выборки на кошелёк (каждая — с новым прокси)
RETRY_BACKOFF_SEC = 1.0  # прямой режим: пауза после сетевой ошибки (× номер неудачи, до 10 с)
RETRY_429_BACKOFF_SEC = 4.0  # прямой режим: пауза после 429 (× номер неудачи, до 30 с)
RETRY_PROXY_BACKOFF_SEC = 0.3  # через прокси: пауза перед повтором — следующий идёт с другого IP
PROXY_COOLDOWN_429_SEC = 15  # прокси, получивший 429/403 от Rabby, не выдаётся столько секунд
RABBY_RETRY_429 = 3  # прямой режим: повторов в клиенте Rabby на 429/5xx (3·6·12 с или Retry-After)
# Работа без прокси. По умолчанию запрещена (прокси обязателен); включается
# переменной окружения DBC_DIRECT=1 или флагом --direct у audit_passes.py.
ALLOW_DIRECT = os.environ.get("DBC_DIRECT", "") in ("1", "true", "yes")
# Глобальный лимит запросов к Rabby в прямом режиме (один IP): ~10 req/s с
# одного адреса приводят к HTTP 403 на /v1/user/total_balance.
RABBY_DIRECT_RATE_PER_SEC = 4.0
# Агрегат total_balance нужен только для контроля; при 403 (в прямом режиме и
# при 429) выборка продолжается без него (aggregate_usd=None). Через прокси
# 429 → выборка повторяется с другого IP, чтобы не терять сверку по сетям.
AGGREGATE_OPTIONAL = True

# Параллелизм
MAX_WORKERS = 500  # максимум одновременно проверяемых кошельков
PROXY_MULTIPLIER = 5  # воркеров = min(MAX_WORKERS, кошельков, прокси × PROXY_MULTIPLIER)
DIRECT_WORKERS = 8  # воркеров в прямом режиме (один IP)
# Выборки одного кошелька (CORROBORATION_MIN_AGREE) идут одновременно через
# разные прокси; выборка, не завершившаяся за SNAPSHOT_HEDGE_SEC, дублируется
# новой («хедж») — берутся первые согласованные.
SNAPSHOT_HEDGE_SEC = 5.0

# Минимальная сумма для отображения (USD)
MIN_VALUE_DISPLAY = 0.01

# --- Защита от «фантомных» балансов -------------------------------------
# С 11.09.2026 бэкенд Rabby отдаёт для части адресов ЧУЖИЕ данные:
#   * /v1/user/total_balance — агрегат с суммами чужого кошелька (одинаковые
#     значения у разных адресов), «липнет» на минуты-часы;
#   * /v1/user/complex_app_list — случайные app-chain позиции (Hyperliquid,
#     Lighter, Polymarket…) с нашим user_addr, но чужими суммами; от запроса к
#     запросу меняются ([] → $18k → $6k → []).
# Список токенов (/v1/user/cache_token_list) при этом стабилен и совпадает с
# независимыми источниками. Поэтому:
#   1. Итог НИКОГДА не берётся из total_usd_value. Итог = токены кошелька
#      + EVM-позиции протоколов (пересчитанные по asset_token_list)
#      + app-chain позиции, подтверждённые нативными API (Hyperliquid, Lighter, Polymarket).
#   2. Агрегат Rabby используется только как контроль: расхождение с
#      проверенными компонентами фиксируется в примечании.
#   3. App-chain позиции из Rabby, которые нельзя проверить нативным API,
#      в итог не входят (учитываются отдельно как unverified_usd).
#   4. Баланс принимается, только если CORROBORATION_MIN_AGREE независимых
#      выборок сошлись по итогу. Иначе статус UNVERIFIED.
COMPONENT_TOL_ABS = 1.0    # абсолютный допуск сверки компонент/агрегата (USD)
COMPONENT_TOL_REL = 0.02   # относительный допуск (2%)
CHAIN_REFETCH_MAX = 6      # сколько сетей перепроверять свежим token_list, если агрегат по сети выше токенов

# Нативные API для app-chain позиций
APPCHAIN_VERIFY = True                 # False — app-chain позиции полностью игнорируются
HL_API_URL = "https://api.hyperliquid.xyz/info"
HL_RATE_PER_SEC = 9.0                  # лимит запросов к Hyperliquid НА ОДИН IP (weight 2 → 1080/мин < 1200)
# Нативные API и RPC ходят через тот же прокси, что и Rabby (если прокси есть):
# лимиты публичных сервисов делятся на все IP, а не упираются в один.
APPCHAIN_VIA_PROXY = True
LIGHTER_API_URL = "https://mainnet.zklighter.elliot.ai/api/v1/account"
APPCHAIN_RATE_PER_SEC = 4.0            # лимит запросов к Polymarket data-api / Lighter на один IP
APPCHAIN_TIMEOUT = 8

# On-chain верификация токенов через публичные JSON-RPC (api/onchain.py):
# токены с оценкой ≥ ONCHAIN_MIN_USD подтверждаются eth_getBalance/balanceOf.
# Фантомный токен (on-chain 0) отбрасывается, расхождение количества
# исправляется по данным сети. Кандидаты из по-сетевого token_list
# принимаются ТОЛЬКО после on-chain подтверждения.
ONCHAIN_VERIFY = True
ONCHAIN_MIN_USD = 0.5
ONCHAIN_RPC_TIMEOUT = 6
ONCHAIN_RPC_HEDGE_SEC = 1.5              # нода молчит дольше — параллельно спрашиваем следующую
ONCHAIN_ROUNDS = 3                     # раундов перебора всех RPC сети до признания сети недоступной
ONCHAIN_AMOUNT_TOL = 0.01              # допуск расхождения количества (1%)
# Токен ≥ ONCHAIN_MIN_USD без on-chain подтверждения → выборка не засчитывается
# (повтор, затем UNVERIFIED). Ничего непроверенного в итог не попадает.

# Rabby иногда отдаёт для кошелька ЧУЖОЙ список токенов целиком (наших в нём
# нет). On-chain проверка снимает чужие токены, но пропажу наших не видит.
# Поэтому выборка не участвует в согласии, если:
#   * on-chain проверка сняла > max(TAINT_ABS_USD, TAINT_REL × стоимость
#     токенов по Rabby) — «заражённая» выборка;
#   * в ней нет токена, подтверждённого на цепочке в другой выборке — «неполная».
TAINT_ABS_USD = 5.0
TAINT_REL = 0.10

CORROBORATION_ENABLED = True      # False — принять первую же выборку
CORROBORATION_MIN_AGREE = 2       # сошедшихся выборок нужно для приёма
CORROBORATION_MAX_FETCHES = 6     # бюджет выборок на кошелёк (включая хеджи)
CORROBORATION_REL_TOL = 0.02      # относительный допуск согласия (2%)
CORROBORATION_ABS_TOL = 1.0       # абсолютный допуск согласия (USD)

# NFT (в сумму не входят). Для кошельков с сотнями коллекций сервер считает
# collection_list 6–20 с, поэтому NFT грузятся в фоне и НЕ задерживают баланс:
# экспорт ждёт их догрузки (checker.wait_nfts). Запрос, не ответивший за
# NFT_HEDGE_SEC, дублируется через другой прокси (до NFT_ATTEMPTS попыток).
NFT_REQUEST_TIMEOUT = 25
NFT_HEDGE_SEC = 12.0
NFT_ATTEMPTS = 3
NFT_BACKGROUND_WORKERS = 256

# Лог сырых данных (logs.txt): писать сырые ответы при отклонённом агрегате /
# фантомных app-chain позициях. Ограничение размера одной записи в байтах.
RAW_LOG_ENABLED = True
RAW_LOG_MAX_BYTES = 20_000

# Отладка
DEBUG = False  # True — отладочное логирование (время, попытки, прокси)
