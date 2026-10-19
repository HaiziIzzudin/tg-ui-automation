import os
import sys
import pytest
from pathlib import Path

# Add the parent directory to sys.path so we can import config
sys.path.insert(0, str(Path(__file__).parent.parent))

import config as config_module

from config import (
    parse_resolution,
    parse_position,
    parse_click_points,
    parse_int,
    parse_float,
    parse_maximized,
    parse_record_mode,
    parse_text,
    parse_log_level,
    parse_recordings_dir,
    Config,
    DEFAULT_RECORDINGS_DIR,
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


class TestParseFloat:
    def test_valid_float(self):
        assert parse_float("3.5", 0.0) == 3.5

    def test_invalid_returns_default(self):
        assert parse_float("abc", 2.5) == 2.5

    def test_negative_returns_default(self):
        assert parse_float("-1.5", 1.0) == 1.0

    def test_zero_returns_default(self):
        assert parse_float("0.0", 1.0) == 1.0

    def test_integer_string(self):
        assert parse_float("5", 0.0) == 5.0


class TestParseMaximized:
    def test_maximized_string(self):
        assert parse_maximized("maximized", (100, 100)) is None

    def test_invalid_returns_default(self):
        assert parse_maximized("invalid", (100, 100)) == (100, 100)

    def test_case_insensitive(self):
        assert parse_maximized("MAXIMIZED", (100, 100)) is None




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
        monkeypatch.delenv("CLICK_RETRY_INTERVAL", raising=False)
        monkeypatch.delenv("RECORD_MODE", raising=False)
        monkeypatch.delenv("RECORD_STREAM_URL", raising=False)
        monkeypatch.delenv("RECORDINGS_DIR", raising=False)
        monkeypatch.delenv("RECORD_CLIP_SECONDS", raising=False)
        monkeypatch.delenv("RECORD_RECONNECT_SECONDS", raising=False)
        monkeypatch.delenv("RECORD_FFMPEG_PATH", raising=False)
        monkeypatch.delenv("RECORD_DISK_WARN_GB", raising=False)
        monkeypatch.delenv("RECORD_DISK_STOP_GB", raising=False)
        monkeypatch.delenv("LOG_LEVEL", raising=False)

        config = Config()
        assert config.second_window_title == "secondWindowTitle"
        assert config.screen_resolution == (1920, 1080)
        assert config.screen_rotation == 0
        assert config.click_points == [(44, 240), (207, 119), (755, 113)]
        assert config.second_window_size == (1280, 1032)
        assert config.second_window_pos == (0, 0)
        assert config.kill_cooldown_seconds == 60
        assert config.second_window_retry_limit == 3
        assert config.click_retry_interval == 3.0
        assert config.recording_mode == "off"
        assert config.record_stream_url == ""
        assert config.recordings_dir == DEFAULT_RECORDINGS_DIR
        assert os.path.isabs(config.recordings_dir)
        assert config.record_clip_seconds == 7200
        assert config.record_reconnect_seconds == 5.0
        assert config.record_ffmpeg_path == "ffmpeg"
        assert config.record_disk_warn_gb == 10
        assert config.record_disk_stop_gb == 2
        assert config.log_level == "INFO"
        monkeypatch.setenv("SECOND_WINDOW_TITLE", "MyWindow")
        monkeypatch.setenv("SCREEN_RESOLUTION", "1080x1920")
        monkeypatch.setenv("SCREEN_ROTATION", "90")
        monkeypatch.setenv("CLICK_POINTS", "10,20;30,40")
        monkeypatch.setenv("SECOND_WINDOW_SIZE", "800x600")
        monkeypatch.setenv("SECOND_WINDOW_POS", "100,200")
        monkeypatch.setenv("KILL_COOLDOWN_SECONDS", "30")
        monkeypatch.setenv("SECOND_WINDOW_RETRY_LIMIT", "5")
        monkeypatch.setenv("CLICK_RETRY_INTERVAL", "1.5")
        monkeypatch.setenv("RECORD_MODE", "follow-schedule")
        monkeypatch.setenv("RECORD_STREAM_URL", "rtmp://example.test/live")
        monkeypatch.setenv("RECORDINGS_DIR", "D:\\Clips")
        monkeypatch.setenv("RECORD_CLIP_SECONDS", "1800")
        monkeypatch.setenv("RECORD_RECONNECT_SECONDS", "12.5")
        monkeypatch.setenv("RECORD_FFMPEG_PATH", "C:\\tools\\ffmpeg.exe")
        monkeypatch.setenv("RECORD_DISK_WARN_GB", "20")
        monkeypatch.setenv("RECORD_DISK_STOP_GB", "5")
        monkeypatch.setenv("LOG_LEVEL", "debug")

        config = Config()
        assert config.second_window_title == "MyWindow"
        assert config.screen_resolution == (1080, 1920)
        assert config.screen_rotation == 90
        assert config.click_points == [(10, 20), (30, 40)]
        assert config.second_window_size == (800, 600)
        assert config.second_window_pos == (100, 200)
        assert config.kill_cooldown_seconds == 30
        assert config.second_window_retry_limit == 5
        assert config.click_retry_interval == 1.5
        assert config.recording_mode == "follow-schedule"
        assert config.record_stream_url == "rtmp://example.test/live"
        assert config.recordings_dir == "D:\\Clips"
        assert config.record_clip_seconds == 1800
        assert config.record_reconnect_seconds == 12.5
        assert config.record_ffmpeg_path == "C:\\tools\\ffmpeg.exe"
        assert config.record_disk_warn_gb == 20
        assert config.record_disk_stop_gb == 5
        assert config.log_level == "DEBUG"

    def test_malformed_values_fallback(self, monkeypatch):
        monkeypatch.setenv("SCREEN_RESOLUTION", "invalid")
        monkeypatch.setenv("SCREEN_ROTATION", "abc")
        monkeypatch.setenv("CLICK_POINTS", "invalid")
        monkeypatch.setenv("SECOND_WINDOW_SIZE", "maximized")
        monkeypatch.setenv("SECOND_WINDOW_POS", "invalid")
        monkeypatch.setenv("KILL_COOLDOWN_SECONDS", "abc")
        monkeypatch.setenv("SECOND_WINDOW_RETRY_LIMIT", "abc")
        monkeypatch.setenv("CLICK_RETRY_INTERVAL", "abc")
        monkeypatch.setenv("RECORD_MODE", "sometimes")
        monkeypatch.setenv("RECORDINGS_DIR", "   ")
        monkeypatch.setenv("RECORD_CLIP_SECONDS", "abc")
        monkeypatch.setenv("RECORD_RECONNECT_SECONDS", "-2")
        monkeypatch.setenv("RECORD_FFMPEG_PATH", "  ")
        monkeypatch.setenv("RECORD_DISK_WARN_GB", "abc")
        monkeypatch.setenv("RECORD_DISK_STOP_GB", "-1")
        monkeypatch.setenv("LOG_LEVEL", "loud")

        config = Config()
        assert config.screen_resolution == (1920, 1080)  # default
        assert config.screen_rotation == 0  # default
        assert config.click_points == [(44, 240), (207, 119), (755, 113)]  # default
        assert config.second_window_size is None  # maximized
        assert config.second_window_pos == (0, 0)  # default
        assert config.kill_cooldown_seconds == 60  # default
        assert config.second_window_retry_limit == 3  # default
        assert config.click_retry_interval == 3.0  # default
        assert config.recording_mode == "off"  # default
        assert config.recordings_dir == DEFAULT_RECORDINGS_DIR  # default
        assert config.record_clip_seconds == 7200  # default
        assert config.record_reconnect_seconds == 5.0  # default
        assert config.record_ffmpeg_path == "ffmpeg"  # default
        assert config.record_disk_warn_gb == 10  # default
        assert config.record_disk_stop_gb == 2  # default
        assert config.log_level == "INFO"  # default

    def test_zero_clip_and_reconnect_fall_back(self, monkeypatch):
        monkeypatch.setenv("RECORD_CLIP_SECONDS", "0")
        monkeypatch.setenv("RECORD_RECONNECT_SECONDS", "0")

        config = Config()
        assert config.record_clip_seconds == 7200
        assert config.record_reconnect_seconds == 5.0


class TestRecordDiskThresholds:
    """The two disk levels are plain gigabytes with a cross-check."""

    def test_defaults_are_ten_and_two(self, monkeypatch):
        monkeypatch.delenv("RECORD_DISK_WARN_GB", raising=False)
        monkeypatch.delenv("RECORD_DISK_STOP_GB", raising=False)

        config = Config()
        assert config.record_disk_warn_gb == 10
        assert config.record_disk_stop_gb == 2

    def test_fractional_gigabytes_are_allowed(self, monkeypatch):
        monkeypatch.setenv("RECORD_DISK_WARN_GB", "0.5")
        monkeypatch.setenv("RECORD_DISK_STOP_GB", "0.25")

        config = Config()
        assert config.record_disk_warn_gb == 0.5
        assert config.record_disk_stop_gb == 0.25

    @pytest.mark.parametrize("bad", ["abc", "0", "-5", "   "])
    def test_bad_warn_value_warns_and_falls_back(self, monkeypatch, caplog, bad):
        monkeypatch.setenv("RECORD_DISK_WARN_GB", bad)
        monkeypatch.setenv("RECORD_DISK_STOP_GB", "2")

        with caplog.at_level("WARNING"):
            config = Config()
        assert config.record_disk_warn_gb == 10
        assert config.record_disk_stop_gb == 2
        assert any("RECORD_DISK_WARN_GB" in r.message or "Invalid float" in r.message
                   for r in caplog.records)

    @pytest.mark.parametrize("bad", ["abc", "0", "-5", "   "])
    def test_bad_stop_value_warns_and_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("RECORD_DISK_WARN_GB", "10")
        monkeypatch.setenv("RECORD_DISK_STOP_GB", bad)

        config = Config()
        assert config.record_disk_warn_gb == 10
        assert config.record_disk_stop_gb == 2

    def test_stop_below_warn_is_kept(self, monkeypatch):
        monkeypatch.setenv("RECORD_DISK_WARN_GB", "30")
        monkeypatch.setenv("RECORD_DISK_STOP_GB", "7")

        config = Config()
        assert config.record_disk_warn_gb == 30
        assert config.record_disk_stop_gb == 7

    def test_stop_equal_to_warn_warns_once_and_uses_both_defaults(self, monkeypatch, caplog):
        monkeypatch.setenv("RECORD_DISK_WARN_GB", "4")
        monkeypatch.setenv("RECORD_DISK_STOP_GB", "4")

        with caplog.at_level("WARNING"):
            config = Config()
        assert config.record_disk_warn_gb == 10
        assert config.record_disk_stop_gb == 2
        warnings = [r for r in caplog.records if "RECORD_DISK_STOP_GB" in r.message]
        assert len(warnings) == 1

    def test_stop_above_warn_warns_once_and_uses_both_defaults(self, monkeypatch, caplog):
        monkeypatch.setenv("RECORD_DISK_WARN_GB", "2")
        monkeypatch.setenv("RECORD_DISK_STOP_GB", "10")

        with caplog.at_level("WARNING"):
            config = Config()
        assert config.record_disk_warn_gb == 10
        assert config.record_disk_stop_gb == 2
        assert any("RECORD_DISK_STOP_GB" in r.message for r in caplog.records)


class TestRecordMode:
    def test_accepts_known_modes(self):
        assert parse_record_mode("off", "off") == "off"
        assert parse_record_mode("long-living", "off") == "long-living"
        assert parse_record_mode("follow-schedule", "off") == "follow-schedule"

    def test_case_and_whitespace_insensitive(self):
        assert parse_record_mode("  LONG-LIVING  ", "off") == "long-living"
        assert parse_record_mode("Follow-Schedule", "off") == "follow-schedule"
        assert parse_record_mode("\tOff\n", "off") == "off"

    def test_unknown_mode_returns_default(self, caplog):
        with caplog.at_level("WARNING"):
            assert parse_record_mode("sometimes", "off") == "off"
        assert "sometimes" in caplog.text
        assert "long-living" in caplog.text

    def test_empty_mode_returns_default(self):
        assert parse_record_mode("", "off") == "off"
        assert parse_record_mode("   ", "off") == "off"

    def test_mode_from_config_is_normalised(self, monkeypatch):
        monkeypatch.setenv("RECORD_MODE", "  Follow-Schedule ")
        assert Config().recording_mode == "follow-schedule"


class TestParseText:
    def test_keeps_trimmed_value(self):
        assert parse_text("  Recordings  ", "fallback") == "Recordings"

    def test_empty_returns_default(self):
        assert parse_text("", "fallback") == "fallback"
        assert parse_text("   ", "fallback") == "fallback"


class TestParseLogLevel:
    """LOG_LEVEL accepts four names, case-insensitively, and warns otherwise."""

    def test_accepts_the_four_names(self):
        assert parse_log_level("DEBUG", "INFO") == "DEBUG"
        assert parse_log_level("INFO", "INFO") == "INFO"
        assert parse_log_level("WARNING", "INFO") == "WARNING"
        assert parse_log_level("ERROR", "INFO") == "ERROR"

    def test_is_case_and_whitespace_insensitive(self):
        assert parse_log_level("debug", "INFO") == "DEBUG"
        assert parse_log_level("  Warning  ", "INFO") == "WARNING"
        assert parse_log_level("\terror\n", "INFO") == "ERROR"

    def test_default_is_info(self):
        assert parse_log_level("DEBUG") == "DEBUG"
        assert parse_log_level("INFO") == "INFO"

    @pytest.mark.parametrize("bad", ["verbose", "", "   ", "TRACE", "critical"])
    def test_nonsense_warns_once_and_falls_back(self, caplog, bad):
        with caplog.at_level("WARNING"):
            result = parse_log_level(bad, "INFO")
        assert result == "INFO"
        warnings = [r for r in caplog.records if "log level" in r.getMessage().lower()]
        assert len(warnings) == 1

    def test_config_reads_and_normalises_the_setting(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "debug")
        assert Config().log_level == "DEBUG"

    def test_config_nonsense_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("LOG_LEVEL", "verbose")
        with caplog.at_level("WARNING"):
            cfg = Config()
        assert cfg.log_level == "INFO"
        assert any("LOG_LEVEL" in r.getMessage() or "log level" in r.getMessage().lower()
                   for r in caplog.records)


class TestParseRecordingsDir:
    """The Clips folder must be absolute; relative or empty falls back."""

    def test_default_is_absolute_next_to_the_program(self):
        assert os.path.isabs(DEFAULT_RECORDINGS_DIR)
        expected = os.path.join(
            os.path.dirname(os.path.abspath(config_module.__file__)), "Recordings"
        )
        assert DEFAULT_RECORDINGS_DIR == expected

    def test_absolute_drive_path_is_kept(self):
        assert parse_recordings_dir(r"D:\Clips", DEFAULT_RECORDINGS_DIR) == r"D:\Clips"

    def test_unc_path_is_kept_unchanged(self):
        unc = r"\\server\share\clips"
        assert parse_recordings_dir(unc, DEFAULT_RECORDINGS_DIR) == unc

    def test_absolute_value_is_trimmed_but_not_otherwise_changed(self):
        assert parse_recordings_dir(r"  D:\Clips  ", DEFAULT_RECORDINGS_DIR) == r"D:\Clips"

    def test_relative_value_warns_once_and_falls_back(self, caplog):
        with caplog.at_level("WARNING"):
            result = parse_recordings_dir("Recordings", DEFAULT_RECORDINGS_DIR)

        assert result == DEFAULT_RECORDINGS_DIR
        warnings = [r for r in caplog.records if "RECORDINGS_DIR" in r.getMessage()]
        assert len(warnings) == 1
        assert "absolute" in warnings[0].getMessage()

    def test_empty_value_warns_once_and_falls_back(self, caplog):
        with caplog.at_level("WARNING"):
            result = parse_recordings_dir("   ", DEFAULT_RECORDINGS_DIR)

        assert result == DEFAULT_RECORDINGS_DIR
        warnings = [r for r in caplog.records if "RECORDINGS_DIR" in r.getMessage()]
        assert len(warnings) == 1
        assert "absolute" in warnings[0].getMessage()

    def test_config_default_is_the_absolute_program_folder(self, monkeypatch):
        monkeypatch.delenv("RECORDINGS_DIR", raising=False)
        cfg = Config()
        assert cfg.recordings_dir == DEFAULT_RECORDINGS_DIR

    def test_config_relative_value_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("RECORDINGS_DIR", "Recordings")
        with caplog.at_level("WARNING"):
            cfg = Config()
        assert cfg.recordings_dir == DEFAULT_RECORDINGS_DIR
        assert any("RECORDINGS_DIR" in r.getMessage() for r in caplog.records)

    def test_config_unc_value_is_kept(self, monkeypatch):
        monkeypatch.setenv("RECORDINGS_DIR", r"\\server\share\clips")
        assert Config().recordings_dir == r"\\server\share\clips"


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
