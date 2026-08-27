"""Выбор цитаты из архива башорг.рф начиная с заданной даты.

RSS отдаёт только ~100 последних цитат, поэтому для полноценного архива
(по умолчанию — с 01.01.2020 и до сегодня) нужны страницы /quote/<id>:
ID цитат растут в порядке публикации, значит диапазон ID однозначно
покрывает нужный период.

* find_archive_start_id() — бинарный поиск границы: наименьший ID, дата
  которого не раньше ARCHIVE_START_DATE; результат кэшируется в хранилище.
* pick_archive_quote() — равномерно берёт случайный ID из диапазона и
  публикует первую валидную цитату (удалённые и уже изданные пропускает).
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime

import httpx

from .feed import FeedError, fetch_quote_page
from .quotes import Quote, parse_page_quote

log = logging.getLogger(__name__)

# Цитаты начиная с этой даты включительно участвуют в пуле.
ARCHIVE_START_DATE = date(2020, 1, 1)

# Насколько вперёд смотрим при пропуске удалённых цитат при поиске границы.
_PROBE_WINDOW = 60
# Сколько случайных кандидатов пробуем за один цикл публикации.
_PICK_ATTEMPTS = 12
# Пауза между запросами во время бинарного поиска границы (вежливость).
_REQUEST_DELAY_SECONDS = 0.2


async def find_archive_start_id(
    client: httpx.AsyncClient,
    site_base_url: str,
    max_id: int,
    *,
    start_id: int = 1,
) -> int | None:
    """Наименьший ID цитаты с датой >= ARCHIVE_START_DATE (или None).

    ID монотонно растут со временем, поэтому граница ищется бинарно по ID.
    Удалённые цитаты сайт отдаёт как заглушку без даты — такие ID
    пропускаем, заглядывая вперёд в пределах _PROBE_WINDOW.
    """
    lo, hi = start_id, max_id
    if lo > hi:
        return None

    bottom = await _resolve_date(client, site_base_url, lo)
    if bottom is None or bottom.date() >= ARCHIVE_START_DATE:
        return lo if (bottom is not None and bottom.date() >= ARCHIVE_START_DATE) else None

    top = await _resolve_date(client, site_base_url, hi)
    if top is None or top.date() < ARCHIVE_START_DATE:
        return None

    checks = 0
    while lo < hi:
        mid = (lo + hi) // 2
        candidate = await _resolve_date(client, site_base_url, mid)
        if candidate is not None and candidate.date() >= ARCHIVE_START_DATE:
            hi = mid
        else:
            lo = mid + 1
        checks += 1
        if checks % 8 == 0:
            await asyncio.sleep(_REQUEST_DELAY_SECONDS)

    found = await _resolve_date(client, site_base_url, lo)
    if found is None or found.date() < ARCHIVE_START_DATE:
        return None
    return lo


async def pick_archive_quote(
    client: httpx.AsyncClient,
    site_base_url: str,
    start_id: int,
    max_id: int,
    seen: set[str],
    rng: random.Random,
    *,
    attempts: int = _PICK_ATTEMPTS,
) -> Quote | None:
    """Случайная неопубликованная цитата из диапазона; None — не вышло.

    Каждый кандидат проверяется: страница существует, дата в пределах
    [ARCHIVE_START_DATE, сегодня], ID ещё не в ``seen``. Удалённые цитаты
    (заглушки без даты) просто пропускаем.
    """
    today = date.today()
    for _ in range(attempts):
        candidate = rng.randint(start_id, max_id)
        if str(candidate) in seen:
            continue
        try:
            page = await fetch_quote_page(client, f"{site_base_url}/quote/{candidate}")
        except FeedError as exc:
            log.warning("Ошибка запроса /quote/%d: %s", candidate, exc)
            return None
        if page is None:
            continue
        quote = parse_page_quote(page, site_base_url)
        if quote is None or quote.published_at is None:
            continue
        published_on = quote.published_at.date()
        if published_on < ARCHIVE_START_DATE or published_on > today:
            continue
        return quote
    return None


async def _resolve_date(
    client: httpx.AsyncClient,
    site_base_url: str,
    quote_id: int,
) -> datetime | None:
    """Дата ближайшей живой цитаты от ``quote_id`` вперёд (в пределах окна).

    None — в окне ни одной живой цитаты либо шуточная заглушка.
    """
    for offset in range(_PROBE_WINDOW):
        candidate = quote_id + offset
        try:
            page = await fetch_quote_page(client, f"{site_base_url}/quote/{candidate}")
        except FeedError:
            return None
        if page is None:
            continue
        quote = parse_page_quote(page, site_base_url)
        if quote is not None and quote.published_at is not None:
            log.debug("ID %d → дата %s", quote_id + offset, quote.published_at)
            return quote.published_at
    return None
