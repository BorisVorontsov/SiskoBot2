"""Загрузка и валидация конфигурации из переменных окружения."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_FEED_URL = "https://башорг.рф/rss/"
DEFAULT_SITE_BASE_URL = "https://башорг.рф"
USER_AGENT = "bashorg-tg-bot/1.0 (self-hosted telegram quote bot)"

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(Exception):
    """Некорректная конфигурация приложения."""


@dataclass(frozen=True)
class Config:
    """Все настройки бота.

    Атрибуты читаются один раз при старте из окружения (.env поддерживается,
    но реальные переменные окружения имеют приоритет).
    """

    token: str
    chat_id: int | None
    min_interval_hours: float
    max_interval_hours: float
    db_path: Path
    feed_url: str
    site_base_url: str
    http_timeout_seconds: float
    log_level: str

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> Config:
        # Явно переданное окружение (тесты) имеет приоритет; иначе читаем
        # переменные окружения, предварительно подгрузив файл .env, если есть.
        if env is None:
            load_dotenv()
            env = os.environ

        token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            raise ConfigError(
                "Переменная TELEGRAM_BOT_TOKEN обязательна. "
                "Создайте токен через @BotFather и укажите его в .env."
            )

        chat_id_raw = (env.get("TELEGRAM_CHAT_ID") or "").strip()
        chat_id: int | None = None
        if chat_id_raw:
            try:
                chat_id = int(chat_id_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"TELEGRAM_CHAT_ID={chat_id_raw!r} не является целым числом "
                    "(для групп обычно отрицательное, например -1001234567890)."
                ) from exc

        min_hours = _float_env(env, "MIN_INTERVAL_HOURS", 2.0)
        max_hours = _float_env(env, "MAX_INTERVAL_HOURS", 6.0)
        if min_hours <= 0 or max_hours < min_hours:
            raise ConfigError(
                "Требуется 0 < MIN_INTERVAL_HOURS <= MAX_INTERVAL_HOURS, "
                f"получено: {min_hours} и {max_hours}."
            )

        timeout = _float_env(env, "HTTP_TIMEOUT_SECONDS", 30.0)
        if timeout <= 0:
            raise ConfigError(f"HTTP_TIMEOUT_SECONDS должен быть > 0, получено {timeout}.")

        log_level = (env.get("LOG_LEVEL") or "INFO").strip().upper()
        if log_level not in VALID_LOG_LEVELS:
            allowed = ", ".join(VALID_LOG_LEVELS)
            raise ConfigError(f"LOG_LEVEL={log_level!r} не поддерживается. Допустимо: {allowed}.")

        return cls(
            token=token,
            chat_id=chat_id,
            min_interval_hours=min_hours,
            max_interval_hours=max_hours,
            db_path=Path((env.get("DB_PATH") or "data/state.db").strip()),
            feed_url=(env.get("FEED_URL") or DEFAULT_FEED_URL).strip(),
            site_base_url=(env.get("SITE_BASE_URL") or DEFAULT_SITE_BASE_URL).strip().rstrip("/"),
            http_timeout_seconds=timeout,
            log_level=log_level,
        )

    @staticmethod
    def mask_token(token: str) -> str:
        """Маскирует токен для безопасного вывода в лог."""
        if len(token) <= 8:
            return "***"
        return f"{token[:3]}…{token[-4:]}"


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} не является числом.") from exc
