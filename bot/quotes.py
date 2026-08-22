"""Модель цитаты, очистка HTML-текста и подготовка сообщений для Telegram.

Сайт отдаёт текст в RSS как экранированный HTML (например ``&lt;br&gt;``),
иногда с двойным экранированием (``&amp;gt;``). Здесь он превращается в
обычный текст с переносами строк, а при отправке — обратно экранируется
под parse_mode=HTML. Никакой HTML/JS из ответа сайта не исполняется.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime

# Официальный лимит длины сообщения Telegram считается в кодовых единицах UTF-16.
TG_MESSAGE_LIMIT = 4096

# Резерв на служебную разметку, добавляемую ботом (жирный заголовок и т.п.),
# чтобы гарантированно не превысить лимит из-за расхождений подсчёта длины.
_MARKUP_RESERVE = 32
_MIN_BODY_BUDGET = 200

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
# Сносим только теги, похожие на настоящие ("<b>", "</i>", "<a href=...>"),
# чтобы не повредить тексты вида «1<2» или «x < y».
_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?>")
_MULTINEWLINE_RE = re.compile(r"\n{3,}")
_QUOTE_ID_RE = re.compile(r"/quote/(\d+)")


@dataclass(frozen=True)
class Quote:
    """Одна цитата сайта."""

    id: str  # числовой идентификатор, например "470009"
    text: str  # очищенный текст с переносами строк
    url: str  # постоянная ссылка на оригинал
    published_at: datetime | None = None  # дата публикации на сайте
    guid: str = ""  # исходный <guid> ленты (страховочный идентификатор)


def utf16_len(text: str) -> int:
    """Длина строки так, как её считает Telegram (в единицах UTF-16)."""
    return len(text.encode("utf-16-le")) // 2


def extract_quote_id(link: str) -> str | None:
    """Достаёт числовой ID цитаты из ссылки вида https://башорг.рф/quote/470009."""
    match = _QUOTE_ID_RE.search(link or "")
    return match.group(1) if match else None


def clean_quote_text(raw: str) -> str:
    """Превращает HTML-содержимое RSS-описания в обычный текст.

    * раскрывает HTML-сущности до стабильного состояния (двойное экранирование);
    * заменяет <br> на переводы строк;
    * удаляет прочие теги;
    * нормализует пустые строки, сохраняя абзацы.
    """
    text = _unescape_until_stable(raw)
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _MULTINEWLINE_RE.sub("\n\n", text)
    return text.strip()


def escape_html(text: str) -> str:
    """Экранирует текст под Telegram parse_mode=HTML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def split_text(text: str, budget: int) -> list[str]:
    """Разбивает текст на куски не длиннее ``budget`` символов UTF-16.

    Предпочтительные точки разреза: пустая строка, перевод строки, пробел.
    Пробелы и переводы строк, не попавшие на границу, сохраняются.
    """
    if budget <= 0:
        raise ValueError("budget должен быть положительным")

    chunks: list[str] = []
    rest = text
    while True:
        limit_index = _budget_prefix(rest, budget)
        if limit_index >= len(rest):
            break  # остаток целиком помещается в лимит
        cut, step = _find_cut(rest[:limit_index])
        chunks.append(rest[:cut].rstrip("\n"))
        rest = rest[cut + step :]
    chunks.append(rest)
    return chunks


def _budget_prefix(text: str, budget: int) -> int:
    """Сколько первых кодовых точек ``text`` укладываются в ``budget`` единиц UTF-16."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > budget:
            return index
    return len(text)


def _find_cut(window: str) -> tuple[int, int]:
    """Возвращает (индекс разреза, сколько символов после него пропустить)."""
    half = max(len(window) // 2, 1)

    cut = window.rfind("\n\n")
    if cut >= half:
        return cut, 2
    cut = window.rfind("\n")
    if cut >= half:
        return cut, 1
    cut = window.rfind(" ")
    if cut >= half:
        return cut, 1
    return len(window), 0


def format_messages(
    quote: Quote,
    limit: int = TG_MESSAGE_LIMIT,
) -> list[str]:
    """Готовит одно или несколько готовых к отправке HTML-сообщений.

    Первое сообщение начинается заголовком «Цитата #ID · дата», последнее
    заканчивается ссылкой на оригинал. Длинные цитаты разбиваются по
    абзацам/строкам — текст никогда не обрезается молча.
    """
    header = f"<b>Цитата #{escape_html(quote.id)}</b>"
    if quote.published_at is not None:
        header += f" · {quote.published_at:%d.%m.%Y в %H:%M}"

    attr_url = quote.url.replace("&", "&amp;")
    footer = f'<i><a href="{attr_url}">оригинал</a></i>'

    body_budget = limit - utf16_len(header) - utf16_len(footer) - 2 * len("\n\n") - _MARKUP_RESERVE
    if body_budget < _MIN_BODY_BUDGET:
        raise ValueError(f"Слишком маленький лимит сообщения: {limit}")

    body = escape_html(quote.text)
    pieces = split_text(body, body_budget)

    messages: list[str] = []
    last = len(pieces) - 1
    for index, piece in enumerate(pieces):
        parts: list[str] = []
        if index == 0:
            parts.append(header)
        parts.append(piece)
        if index == last:
            parts.append(footer)
        messages.append("\n\n".join(parts))
    return messages


def _unescape_until_stable(raw: str, max_rounds: int = 5) -> str:
    """Раскручивает многократное HTML-экранирование (&amp;gt; → &gt; → >)."""
    current = raw
    for _ in range(max_rounds):
        unescaped = html.unescape(current)
        if unescaped == current:
            break
        current = unescaped
    return current
