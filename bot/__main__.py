"""Точка входа: python -m bot [--once]"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import time

import httpx
from telegram.error import Conflict, InvalidToken

from . import __version__
from .app import App
from .config import USER_AGENT, Config, ConfigError
from .telegram_api import TelegramClient

log = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    formatter.converter = time.gmtime  # логи в UTC — единое время на сервере

    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Чужие библиотеки не должны засорять лог на уровне INFO.
    for noisy in ("httpx", "httpcore", "telegram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _run(cfg: Config, once: bool) -> int:
    app = App(cfg)
    try:
        async with (
            TelegramClient(cfg.token) as client,
            httpx.AsyncClient(
                timeout=httpx.Timeout(cfg.http_timeout_seconds),
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            ) as http_client,
        ):
            log.info(
                "Бот запущен (v%s), токен %s, интервал %.1f–%.1f ч, база: %s",
                __version__,
                Config.mask_token(cfg.token),
                cfg.min_interval_hours,
                cfg.max_interval_hours,
                cfg.db_path,
            )
            await app.run(client, http_client, once=once)
        return 0
    finally:
        app.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot", description="Telegram-бот с цитатами башорг.рф")
    parser.add_argument(
        "--once",
        action="store_true",
        help="выполнить один цикл публикации и выйти",
    )
    args = parser.parse_args(argv)

    try:
        cfg = Config.load()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    _setup_logging(cfg.log_level)

    async def runner() -> int:
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        # SIGTERM корректно останавливает контейнер/сервис (на POSIX).
        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is not None and hasattr(loop, "add_signal_handler"):
                # На Windows add_signal_handler недоступен — там хватит SIGINT/KeyboardInterrupt.
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, task.cancel)
        try:
            await _run(cfg, once=args.once)
            return 0
        except asyncio.CancelledError:
            log.info("Остановка по сигналу — состояние сохранено в базе")
            return 0

    try:
        return asyncio.run(runner())
    except InvalidToken:
        # Сообщение об ошибке PTB содержит сам токен — в лог его нельзя.
        log.critical("Telegram отклонил токен: проверьте TELEGRAM_BOT_TOKEN")
        return 3
    except Conflict:
        log.critical(
            "Этот токен уже используется другим запущенным экземпляром бота. "
            "Остановите второй экземпляр и повторите запуск."
        )
        return 4
    except KeyboardInterrupt:  # pragma: no cover — Windows/редкие пути
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
