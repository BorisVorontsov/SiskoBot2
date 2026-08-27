"""Основной цикл приложения: архив → случайный выбор → публикация → пауза.

Источник цитат — архив башорг.рф начиная с 01.01.2020 (bot/archive.py):
бот равномерно выбирает ID из диапазона [граница 2020 года … сегодня]
и публикует первую валидную цитату. RSS при этом нужен только для
определения «самого свежего» ID. Во время паузы бот слушает команды
в группе и в личных сообщениях:
* /help — список команд;
* /next — внеочередная публикация новой цитаты.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta

import httpx
from telegram import Update

from .archive import (
    ARCHIVE_START_DATE,
    find_archive_start_id,
    pick_archive_quote,
)
from .config import Config
from .feed import FeedError, fetch_feed, parse_feed
from .quotes import Quote, format_messages
from .scheduler import random_interval
from .storage import Storage
from .telegram_api import BotKickedError, TelegramClient, bot_removed_from

log = logging.getLogger(__name__)

# Если новых постов нет или сайт недоступен — повторяем попытку не сразу,
# а через такую паузу (сайт не нагружаем).
_EMPTY_RETRY_MINUTES = 30.0
# После неудачной публикации (Telegram/сеть) — экспоненциальная пауза.
_ERROR_BACKOFF_START_MINUTES = 5.0
_ERROR_BACKOFF_CAP_MINUTES = 60.0
# Пауза между повторными автоопределениями чата (анти-зацикливание).
_RECOVERY_PAUSE_START = timedelta(seconds=30)
_RECOVERY_PAUSE_CAP = timedelta(minutes=10)

STATUS_PUBLISHED = "published"
STATUS_NOTHING_NEW = "nothing_new"
STATUS_FEED_ERROR = "feed_error"

# Чем закончилась пауза с прослушиванием.
PAUSE_EXPIRED = "expired"  # время вышло — пора публиковать по расписанию
PAUSE_NEXT = "next"  # команда /next — публикация вне очереди
PAUSE_KICKED = "kicked"  # бота удалили из целевого чата — нужен новый чат

HELP_TEXT = (
    "<b>Команды бота:</b>\n"
    "/help — показать этот список\n"
    "/next — опубликовать новую цитату прямо сейчас"
)


def parse_command(text: str | None) -> tuple[str | None, str]:
    """Разбирает текст сообщения в пару (команда, аргумент).

    В группах Telegram дописывает имя бота («/next@MyBot»), поэтому суффикс
    после «@» отбрасывается. Не-команды дают (None, '').
    """
    if not text or not text.startswith("/"):
        return None, ""
    head, _, argument = text.partition(" ")
    command = head[1:].split("@", 1)[0].lower()
    return command, argument.strip()


class App:
    """Связывает все компоненты и крутит бесконечный цикл публикаций."""

    def __init__(self, cfg: Config, *, rng: random.Random | None = None):
        self.cfg = cfg
        self.rng = rng if rng is not None else random.SystemRandom()
        self.storage = Storage(cfg.db_path)

    def close(self) -> None:
        self.storage.close()

    async def run(
        self,
        client: TelegramClient,
        http: httpx.AsyncClient,
        *,
        once: bool = False,
    ) -> None:
        chat_id = await self._resolve_chat_id(client)
        chat_from_env = self.cfg.chat_id is not None
        error_backoff_minutes = _ERROR_BACKOFF_START_MINUTES
        # Счётчик подряд идущих киков: защита от зацикливания «кик → автоопределение».
        consecutive_kicks = 0

        if chat_from_env or self.storage.get_chat_id() is not None:
            # Целевой чат известен — устаревшие события членства в очереди
            # (давние добавления/удаления) сбрасываем, чтобы они не переигрывали.
            await client.flush_pending_updates()

        while True:
            try:
                status = await self.publish_cycle(client, http, chat_id)
            except BotKickedError as exc:
                if chat_from_env:
                    # Явно заданный TELEGRAM_CHAT_ID неверен — автоматически
                    # «угадывать» группу нельзя, требуется действие человека.
                    self.storage.close()
                    raise SystemExit(
                        f"{exc}. Проверьте TELEGRAM_CHAT_ID или уберите его из .env."
                    ) from exc
                log.warning("Бота удалили из чата %s: %s", chat_id, exc)
                await self._pause_between_recoveries(consecutive_kicks)
                consecutive_kicks += 1
                chat_id = await self._recover_chat(client)
                continue
            except Exception:  # сервис обязан переживать любые сбои, включая баги
                log.exception("Неожиданная ошибка цикла публикации")
                status = STATUS_FEED_ERROR

            if once:
                log.info("Режим --once: цикл завершён со статусом %s", status)
                return

            if status == STATUS_PUBLISHED:
                delay = random_interval(
                    self.cfg.min_interval_hours, self.cfg.max_interval_hours, self.rng
                )
                next_at = datetime.now().astimezone() + delay
                log.info(
                    "Следующая публикация через %d ч %02d мин (в ~%s)",
                    delay // timedelta(hours=1),
                    (delay.seconds // 60) % 60,
                    next_at.strftime("%Y-%m-%d %H:%M"),
                )
            elif status == STATUS_NOTHING_NEW:
                log.info("Новых цитат нет; повторю попытку через %.0f мин", _EMPTY_RETRY_MINUTES)
                delay = timedelta(minutes=_EMPTY_RETRY_MINUTES)
            else:
                log.warning(
                    "Цикл завершился ошибкой; следующая попытка через %.0f мин",
                    error_backoff_minutes,
                )
                delay = timedelta(minutes=error_backoff_minutes)

            # Во время любой паузы бот слушает команды (/help, /next).
            outcome = await self._pause_and_listen(client, chat_id, delay)
            if outcome == PAUSE_KICKED:
                if chat_from_env:
                    self.storage.close()
                    raise SystemExit(
                        "Бота удалили из чата TELEGRAM_CHAT_ID. "
                        "Добавьте его обратно или исправьте .env."
                    )
                await self._pause_between_recoveries(consecutive_kicks)
                consecutive_kicks += 1
                chat_id = await self._recover_chat(client)
            elif outcome in (PAUSE_EXPIRED, PAUSE_NEXT) and status == STATUS_PUBLISHED:
                consecutive_kicks = 0

            if status == STATUS_PUBLISHED:
                error_backoff_minutes = _ERROR_BACKOFF_START_MINUTES
            else:
                error_backoff_minutes = min(error_backoff_minutes * 2, _ERROR_BACKOFF_CAP_MINUTES)

    @staticmethod
    async def _pause_between_recoveries(consecutive_kicks: int) -> None:
        """Пауза перед повторным автоопределением: не долбим Telegram и сайт.

        Первый кик обрабатывается сразу; каждый следующий подряд — вдвое дольше,
        потолок 10 минут. Счётчик сбрасывается успешной публикацией.
        """
        if consecutive_kicks <= 0:
            return
        pause = min(_RECOVERY_PAUSE_START * 2 ** (consecutive_kicks - 1), _RECOVERY_PAUSE_CAP)
        log.warning("Повторное автоопределение через %.0f сек", pause.total_seconds())
        await asyncio.sleep(pause.total_seconds())

    async def _recover_chat(self, client: TelegramClient) -> int:
        """Забывает старый чат и ждёт добавления бота в новую группу."""
        self.storage.delete_chat_id()
        log.warning("Перехожу в режим автоопределения: добавьте бота в нужную группу")
        chat_id = await client.discover_chat_id()
        self.storage.set_chat_id(chat_id)
        log.info("Новый целевой чат: %s — продолжаю работу", chat_id)
        return chat_id

    async def _pause_and_listen(
        self,
        client: TelegramClient,
        chat_id: int,
        delay: timedelta,
    ) -> str:
        """Пауза до следующего цикла с прослушиванием обновлений.

        Команды принимаются и в группе, и в личных сообщениях боту — личка
        работает даже при включённом privacy mode. Курсор обновлений ведёт
        клиент. Возврат: PAUSE_EXPIRED — время вышло; PAUSE_NEXT — команда
        /next; PAUSE_KICKED — бота удалили из целевого чата.
        """
        deadline = datetime.now().astimezone() + delay
        while True:
            remaining = (deadline - datetime.now().astimezone()).total_seconds()
            if remaining <= 0:
                return PAUSE_EXPIRED
            timeout = min(remaining, float(client.poll_timeout))
            updates = await client.poll_updates(timeout=timeout)
            for update in updates:
                if bot_removed_from(update, chat_id):
                    log.warning("Бот удалён из целевого чата %s", chat_id)
                    return PAUSE_KICKED
                action, reply_to = self._command_action(update)
                if action == "next":
                    log.info("Команда /next: публикую вне очереди")
                    return PAUSE_NEXT
                if action == "help" and reply_to is not None:
                    await client.send_messages(reply_to, [HELP_TEXT])

    @staticmethod
    def _command_action(update: Update) -> tuple[str | None, int | None]:
        """('help' | 'next', куда отвечать) для команд; всё остальное — (None, None).

        Отвечать нужно туда, откуда пришла команда: в группу или в личку.
        """
        message = update.message
        text = getattr(message, "text", None) if message is not None else None
        command, _argument = parse_command(text)
        if command in ("help", "next") and message is not None:
            return command, int(message.chat.id)
        return None, None

    async def _resolve_chat_id(self, client: TelegramClient) -> int:
        """CHAT_ID из окружения → из хранилища → автоопределение через long polling."""
        env_chat_id = self.cfg.chat_id
        stored_chat_id = self.storage.get_chat_id()

        if env_chat_id is not None:
            if stored_chat_id not in (None, env_chat_id):
                log.info(
                    "TELEGRAM_CHAT_ID=%s имеет приоритет над сохранённым (%s)",
                    env_chat_id,
                    stored_chat_id,
                )
            return env_chat_id

        if stored_chat_id is not None:
            log.info("Использую сохранённый CHAT_ID=%s из хранилища", stored_chat_id)
            return stored_chat_id

        chat_id = await client.discover_chat_id()
        self.storage.set_chat_id(chat_id)
        log.info("Определённый чат %s сохранён в хранилище", chat_id)
        return chat_id

    async def publish_cycle(
        self,
        client: TelegramClient,
        http: httpx.AsyncClient,
        chat_id: int,
    ) -> str:
        """Одна попытка опубликовать одну цитату из архива.

        Возвращает статус; исключения Telegram пробрасываются наверх.
        Цитата помечается опубликованной строго после успешной отправки.
        Граница архива (первая цитата с датой ARCHIVE_START_DATE) ищется
        при первом запуске и запоминается в хранилище.
        """
        try:
            xml_text = await fetch_feed(http, self.cfg.feed_url)
            quotes = parse_feed(xml_text, self.cfg.site_base_url)
        except FeedError as exc:
            log.warning("Лента недоступна или некорректна: %s", exc)
            return STATUS_FEED_ERROR

        if not quotes:
            log.warning("Лента пуста — сайт отдал 0 элементов")
            return STATUS_FEED_ERROR

        ids = [int(q.id) for q in quotes if q.id.isdigit()]
        if not ids:
            log.warning("В ленте нет цитат с числовым ID — не от чего считать границу")
            return STATUS_FEED_ERROR
        max_id = max(ids)
        seen = self.storage.published_ids()

        start_id = self.storage.get_archive_start_id()
        if start_id is None:
            start_id = await self._ensure_archive_start(http, max_id)
            if start_id is None:
                return await self._publish_from_feed(client, chat_id, quotes, seen)

        try:
            quote = await pick_archive_quote(
                http, self.cfg.site_base_url, start_id, max_id, seen, self.rng
            )
        except FeedError as exc:
            log.warning("Ошибка архива: %s", exc)
            return STATUS_FEED_ERROR

        if quote is None:
            log.info(
                "Цитат из архива #%d..#%d не нашлось (уже опубликовано: %d)",
                start_id,
                max_id,
                len(seen),
            )
            return STATUS_NOTHING_NEW

        log.info("Выбрана цитата #%s из архива (дата на сайте: %s)", quote.id, quote.published_at)
        return await self._publish(client, chat_id, quote)

    async def _ensure_archive_start(self, http: httpx.AsyncClient, max_id: int) -> int | None:
        """Ищет и запоминает ID первой цитаты от ARCHIVE_START_DATE; None — не вышло."""
        log.info("Граница архива ещё не найдена: ищу начало с %s", ARCHIVE_START_DATE.isoformat())
        try:
            start_id = await find_archive_start_id(http, self.cfg.site_base_url, max_id)
        except FeedError as exc:
            log.warning("Не удалось определить границу архива: %s", exc)
            return None
        if start_id is None:
            log.warning("Граница архива не найдена — сайт не отдаёт таких старых цитат?")
            return None
        self.storage.set_archive_start_id(start_id)
        log.info("Граница архива: цитата #%d (с %s)", start_id, ARCHIVE_START_DATE.isoformat())
        return start_id

    async def _publish_from_feed(
        self,
        client: TelegramClient,
        chat_id: int,
        quotes: list[Quote],
        seen: set[str],
    ) -> str:
        """Резерв: при сбое поиска границы архива публикуем из свежей ленты."""
        fresh = [quote for quote in quotes if quote.id not in seen]
        log.info(
            "Всего в ленте %d, уже публиковалось %d, новых %d", len(quotes), len(seen), len(fresh)
        )
        if not fresh:
            return STATUS_NOTHING_NEW
        quote = self.rng.choice(fresh)
        log.info("Выбрана цитата #%s из %d новых (резерв ленты)", quote.id, len(fresh))
        return await self._publish(client, chat_id, quote)

    async def _publish(
        self,
        client: TelegramClient,
        chat_id: int,
        quote: Quote,
    ) -> str:
        """Отправка в чат и отметка в хранилище (только после успеха)."""
        messages = format_messages(quote)
        await client.send_messages(chat_id, messages)
        self.storage.mark_published(quote.id)
        log.info(
            "Опубликовано #%s; всего опубликовано: %d",
            quote.id,
            self.storage.count_published(),
        )
        return STATUS_PUBLISHED
