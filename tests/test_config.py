import os
import sys
import pytest
from pathlib import Path

# Add the parent directory to sys.path so we can import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    parse_resolution,
    parse_position,
    parse_click_points,
    parse_int,
    parse_maximized,
    parse_bool,
    Config,
    clamp_rotation,
)


class TestParseResolution:
    def test_valid_resolution(self):
        assert parse_resolution("1920x1080", (0, 0)) == (1920, 1080)

    def test_invalid_format_returns_default(self):
        assert parse_resolution("invalid", (100, 200)) == (100, 200)

    def test_non_numeric_returns_default(self):
        assert parse_resolution("abcxdef", (100, 200)) == (100, 200)

    def test_negative_values_returns_default(self):
        assert parse_resolution("-10x20", (100, 200)) == (100, 200)

    def test_zero_values_returns_default(self):
        assert parse_resolution("0x0", (100, 200)) == (100, 200)

    def test_missing_height_returns_default(self):
        assert parse_resolution("1920", (100, 200)) == (100, 200)


class TestParsePosition:
    def test_valid_position(self):
        assert parse_position("10,20", (0, 0)) == (10, 20)

    def test_invalid_format_returns_default(self):
        assert parse_position("invalid", (50, 60)) == (50, 60)

    def test_non_numeric_returns_default(self):
        assert parse_position("abc,def", (50, 60)) == (50, 60)

    def test_negative_values_allowed(self):
        assert parse_position("-10,20", (0, 0)) == (-10, 20)


class TestParseClickPoints:
    def test_valid_points(self):
        assert parse_click_points("44,240;207,119;755,113", []) == [
            (44, 240),
            (207, 119),
            (755, 113),
        ]

    def test_single_point(self):
        assert parse_click_points("10,20", []) == [(10, 20)]

    def test_empty_string_returns_default(self):
        assert parse_click_points("", [(0, 0)]) == [(0, 0)]

    def test_invalid_point_returns_default(self):
        assert parse_click_points("invalid", [(0, 0)]) == [(0, 0)]

    def test_partial_invalid_returns_default(self):
        assert parse_click_points("10,20;invalid", [(0, 0)]) == [(0, 0)]


class TestParseInt:
    def test_valid_int(self):
        assert parse_int("60", 0) == 60

    def test_invalid_returns_default(self):
        assert parse_int("abc", 30) == 30

    def test_negative_returns_default(self):
        assert parse_int("-5", 10) == 10

    def test_zero_returns_default(self):
        assert parse_int("0", 10) == 10

    def test_float_returns_default(self):
        assert parse_int("3.14", 10) == 10


class TestParseMaximized:
    def test_maximized_string(self):
        assert parse_maximized("maximized", (100, 100)) is None

    def test_invalid_returns_default(self):
        assert parse_maximized("invalid", (100, 100)) == (100, 100)

    def test_case_insensitive(self):
        assert parse_maximized("MAXIMIZED", (100, 100)) is None


class TestParseBool:
    def test_true_variants(self):
        assert parse_bool("true", False) is True
        assert parse_bool("yes", False) is True
        assert parse_bool("1", False) is True

    def test_false_variants(self):
        assert parse_bool("false", True) is False
        assert parse_bool("no", True) is False
        assert parse_bool("0", True) is False

    def test_invalid_returns_default(self):
        assert parse_bool("invalid", True) is True


class TestConfig:
    def test_default_values(self, monkeypatch):
        # Ensure no env vars are set
        monkeypatch.delenv("SECOND_WINDOW_TITLE", raising=False)
        monkeypatch.delenv("SCREEN_RESOLUTION", raising=False)
        monkeypatch.delenv("SCREEN_ROTATION", raising=False)
        monkeypatch.delenv("CLICK_POINTS", raising=False)
        monkeypatch.delenv("SECOND_WINDOW_SIZE", raising=False)
        monkeypatch.delenv("SECOND_WINDOW_POS", raising=False)
        monkeypatch.delenv("KILL_COOLDOWN_SECONDS", raising=False)
        monkeypatch.delenv("SECOND_WINDOW_RETRY_LIMIT", raising=False)

        config = Config()
        assert config.second_window_title == "secondWindowTitle"
        assert config.screen_resolution == (1920, 1080)
        assert config.screen_rotation == 0
        assert config.click_points == [(44, 240), (207, 119), (755, 113)]
        assert config.second_window_size == (1280, 1032)
        assert config.second_window_pos == (0, 0)
        assert config.kill_cooldown_seconds == 60
        assert config.second_window_retry_limit == 3

    def test_custom_values(self, monkeypatch):
        monkeypatch.setenv("SECOND_WINDOW_TITLE", "MyWindow")
        monkeypatch.setenv("SCREEN_RESOLUTION", "1080x1920")
        monkeypatch.setenv("SCREEN_ROTATION", "90")
        monkeypatch.setenv("CLICK_POINTS", "10,20;30,40")
        monkeypatch.setenv("SECOND_WINDOW_SIZE", "800x600")
        monkeypatch.setenv("SECOND_WINDOW_POS", "100,200")
        monkeypatch.setenv("KILL_COOLDOWN_SECONDS", "30")
        monkeypatch.setenv("SECOND_WINDOW_RETRY_LIMIT", "5")

        config = Config()
        assert config.second_window_title == "MyWindow"
        assert config.screen_resolution == (1080, 1920)
        assert config.screen_rotation == 90
        assert config.click_points == [(10, 20), (30, 40)]
        assert config.second_window_size == (800, 600)
        assert config.second_window_pos == (100, 200)
        assert config.kill_cooldown_seconds == 30
        assert config.second_window_retry_limit == 5

    def test_malformed_values_fallback(self, monkeypatch):
        monkeypatch.setenv("SCREEN_RESOLUTION", "invalid")
        monkeypatch.setenv("SCREEN_ROTATION", "abc")
        monkeypatch.setenv("CLICK_POINTS", "invalid")
        monkeypatch.setenv("SECOND_WINDOW_SIZE", "maximized")
        monkeypatch.setenv("SECOND_WINDOW_POS", "invalid")
        monkeypatch.setenv("KILL_COOLDOWN_SECONDS", "abc")
        monkeypatch.setenv("SECOND_WINDOW_RETRY_LIMIT", "abc")

        config = Config()
        assert config.screen_resolution == (1920, 1080)  # default
        assert config.screen_rotation == 0  # default
        assert config.click_points == [(44, 240), (207, 119), (755, 113)]  # default
        assert config.second_window_size is None  # maximized
        assert config.second_window_pos == (0, 0)  # default
        assert config.kill_cooldown_seconds == 60  # default
        assert config.second_window_retry_limit == 3  # default


class TestClampRotation:
    def test_valid_rotations_unchanged(self):
        assert clamp_rotation(0) == 0
        assert clamp_rotation(90) == 90
        assert clamp_rotation(180) == 180
        assert clamp_rotation(270) == 270

    def test_invalid_rotation_clamps_to_0(self):
        assert clamp_rotation(45) == 0
        assert clamp_rotation(360) == 0
        assert clamp_rotation(-90) == 0


class TestGetExpectedResolution:
    def test_rotation_0_no_swap(self, monkeypatch):
        monkeypatch.setenv("SCREEN_RESOLUTION", "1920x1080")
        monkeypatch.setenv("SCREEN_ROTATION", "0")
        cfg = Config()
        assert cfg.get_expected_resolution() == (1920, 1080)

    def test_rotation_90_swaps(self, monkeypatch):
        monkeypatch.setenv("SCREEN_RESOLUTION", "1920x1080")
        monkeypatch.setenv("SCREEN_ROTATION", "90")
        cfg = Config()
        assert cfg.get_expected_resolution() == (1080, 1920)

    def test_rotation_180_no_swap(self, monkeypatch):
        monkeypatch.setenv("SCREEN_RESOLUTION", "1920x1080")
        monkeypatch.setenv("SCREEN_ROTATION", "180")
        cfg = Config()
        assert cfg.get_expected_resolution() == (1920, 1080)

    def test_rotation_270_swaps(self, monkeypatch):
        monkeypatch.setenv("SCREEN_RESOLUTION", "1920x1080")
        monkeypatch.setenv("SCREEN_ROTATION", "270")
        cfg = Config()
        assert cfg.get_expected_resolution() == (1080, 1920)

    def test_invalid_rotation_clamped_to_0(self, monkeypatch):
        monkeypatch.setenv("SCREEN_RESOLUTION", "1920x1080")
        monkeypatch.setenv("SCREEN_ROTATION", "45")
        cfg = Config()
        assert cfg.screen_rotation == 0
        assert cfg.get_expected_resolution() == (1920, 1080)

    def test_custom_resolution_with_rotation(self, monkeypatch):
        monkeypatch.setenv("SCREEN_RESOLUTION", "1080x1920")
        monkeypatch.setenv("SCREEN_ROTATION", "90")
        cfg = Config()
        assert cfg.get_expected_resolution() == (1920, 1080)