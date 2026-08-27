"""Получение и разбор RSS-ленты и страниц цитат башорг.рф.

Приоритетный источник данных — официальный RSS (https://башорг.рф/rss/),
100 последних цитат. Это структурированный источник, поддерживаемый самим
сайтом, поэтому HTML-парсинг и headless-браузеры не требуются.

RSS также даёт максимальный ID цитаты на сегодня; сами исторические цитаты
добираются со страниц /quote/<id> (см. bot/archive.py).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx

from .quotes import Quote, clean_quote_text, extract_quote_id

log = logging.getLogger(__name__)


class FeedError(Exception):
    """Сетевая ошибка или некорректный ответ сайта."""


async def fetch_feed(client: httpx.AsyncClient, url: str) -> str:
    """Забирает XML ленты готовым клиентом, сетевые проблемы → FeedError."""
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise FeedError(f"HTTP {exc.response.status_code} при запросе {url}") from exc
    except httpx.HTTPError as exc:
        raise FeedError(f"Сетевая ошибка при запросе {url}: {exc!r}") from exc
    return response.text


async def fetch_quote_page(client: httpx.AsyncClient, url: str) -> str | None:
    """Забирает HTML-страницу цитаты.

    Возвращает None для 404 — это «удалённая» цитата, которую надо
    пропустить, а не фатальная ошибка. Прочие HTTP/сетевые проблемы → FeedError.
    """
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise FeedError(f"Сетевая ошибка при запросе {url}: {exc!r}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise FeedError(f"HTTP {response.status_code} при запросе {url}")
    return response.text


def parse_feed(xml_text: str, site_base_url: str) -> list[Quote]:
    """Разбирает RSS в список цитат (новые первыми, как в ленте).

    Элементы без текста или без определимого ID пропускаются с warning,
    а не валят весь разбор — сайт мог добавить новый тип записи.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        snippet = " ".join(xml_text[:200].split())
        raise FeedError(f"Некорректный XML от сайта: {exc}; начало ответа: {snippet!r}") from exc

    channel = root.find("channel")
    if channel is None:
        raise FeedError("В ответе сайта нет элемента <channel> — структура RSS изменилась?")

    quotes: list[Quote] = []
    skipped = 0
    for item in channel.iter("item"):
        quote = _parse_item(item, site_base_url)
        if quote is None:
            skipped += 1
        else:
            quotes.append(quote)

    if skipped:
        log.warning("Пропущено элементов ленты без ID или текста: %d", skipped)
    log.info("Из ленты разобрано цитат: %d", len(quotes))
    return quotes


def _parse_item(item: ET.Element, site_base_url: str) -> Quote | None:
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    description = item.findtext("description")
    pub_date = (item.findtext("pubDate") or "").strip()
    guid = (item.findtext("guid") or "").strip()

    label = title or guid or "<без заголовка>"
    if description is None or not description.strip():
        log.warning("У элемента ленты %s нет описания — пропущен", label)
        return None

    quote_id = extract_quote_id(link) or guid
    if not quote_id:
        log.warning("Не удалось определить ID элемента %s — пропущен", label)
        return None

    published_at = None
    if pub_date:
        try:
            published_at = parsedate_to_datetime(pub_date)
        except (TypeError, ValueError):
            log.warning("Не удалось разобрать дату %r у цитаты %s", pub_date, quote_id)

    return Quote(
        id=quote_id,
        text=clean_quote_text(description),
        url=link or f"{site_base_url}/quote/{quote_id}",
        published_at=published_at,
        guid=guid,
    )
