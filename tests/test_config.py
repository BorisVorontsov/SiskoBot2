"""Тесты конфигурации: обязательность токена, дефолты, валидация."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.config import Config, ConfigError

VALID_ENV = {
    "TELEGRAM_BOT_TOKEN": "1234567890:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll",
    "TELEGRAM_CHAT_ID": "-1001234567890",
}


class TestLoad:
    def test_token_required(self):
        with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
            Config.load({})

    def test_blank_token_rejected(self):
        with pytest.raises(ConfigError):
            Config.load({"TELEGRAM_BOT_TOKEN": "   "})

    def test_defaults_match_task_requirements(self):
        cfg = Config.load({"TELEGRAM_BOT_TOKEN": VALID_ENV["TELEGRAM_BOT_TOKEN"]})
        assert cfg.min_interval_hours == 2.0
        assert cfg.max_interval_hours == 6.0
        assert cfg.chat_id is None
        assert cfg.db_path == Path("data/state.db")
        assert cfg.feed_url == "https://башорг.рф/rss/"
        assert cfg.log_level == "INFO"

    def test_chat_id_negative_parsed(self):
        cfg = Config.load(VALID_ENV)
        assert cfg.chat_id == -1001234567890

    def test_chat_id_not_int_rejected(self):
        env = {**VALID_ENV, "TELEGRAM_CHAT_ID": "@username"}
        with pytest.raises(ConfigError, match="TELEGRAM_CHAT_ID"):
            Config.load(env)

    def test_min_greater_than_max_rejected(self):
        env = {**VALID_ENV, "MIN_INTERVAL_HOURS": "7", "MAX_INTERVAL_HOURS": "6"}
        with pytest.raises(ConfigError, match="MAX_INTERVAL_HOURS"):
            Config.load(env)

    def test_zero_min_rejected(self):
        env = {**VALID_ENV, "MIN_INTERVAL_HOURS": "0"}
        with pytest.raises(ConfigError):
            Config.load(env)

    def test_float_intervals_accepted(self):
        env = {**VALID_ENV, "MIN_INTERVAL_HOURS": "1,5"}
        cfg = Config.load(env)
        assert cfg.min_interval_hours == 1.5

    def test_bad_log_level_rejected(self):
        env = {**VALID_ENV, "LOG_LEVEL": "VERBOSE"}
        with pytest.raises(ConfigError, match="LOG_LEVEL"):
            Config.load(env)

    def test_trailing_slash_stripped_from_base_url(self):
        env = {**VALID_ENV, "SITE_BASE_URL": "https://башорг.рф/"}
        assert Config.load(env).site_base_url == "https://башорг.рф"


class TestMaskToken:
    def test_mask_hides_secret_part(self):
        masked = Config.mask_token(VALID_ENV["TELEGRAM_BOT_TOKEN"])
        assert "AAAbbb" not in masked
        assert masked.startswith("123")
        assert masked.endswith("Klll")

    def test_short_token_fully_masked(self):
        assert Config.mask_token("abc12") == "***"
