"""Смоук-тест против живого сайта: python scripts/smoke_live.py"""

import asyncio
import sys

import httpx

from bot.config import USER_AGENT
from bot.feed import FeedError, fetch_feed, parse_feed
from bot.quotes import format_messages

FEED_URL = "https://башорг.рф/rss/"
SITE_BASE = "https://башорг.рф"


async def main() -> int:
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        xml_text = await fetch_feed(client, FEED_URL)
    quotes = parse_feed(xml_text, SITE_BASE)
    print(f"OK: разобрано цитат: {len(quotes)}")
    first = quotes[0]
    print(f"ID={first.id} date={first.published_at} url={first.url}")
    print("--- сообщение для Telegram ---")
    for message in format_messages(first):
        print(message)
        print("---")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except FeedError as exc:
        print(f"FEED ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
