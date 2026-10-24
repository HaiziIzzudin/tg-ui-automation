"""Tests for the List Recorder's settings and the Clean Name rule.

Every test hands ``load_streams_settings`` a plain mapping, so the real
environment and the real ``.env`` file are never read here. Nothing touches
the network, the clock, the disk, or the recorder program.
"""
import dataclasses
import os
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

# Add the parent directory to sys.path so we can import streams_config
sys.path.insert(0, str(Path(__file__).parent.parent))

import config as config_module
from streams_config import (
    DEFAULT_LIST_URL,
    DEFAULT_POLL_SECONDS,
    DEFAULT_CLIP_SECONDS,
    DEFAULT_RECONNECT_SECONDS,
    DEFAULT_RETRY_LIMIT,
    DEFAULT_RETRY_RESET_SECONDS,
    DEFAULT_FFMPEG_PATH,
    DEFAULT_DISK_WARN_GB,
    DEFAULT_DISK_STOP_GB,
    DEFAULT_LOG_LEVEL,
    NAME_MAX_LENGTH,
    StreamsSettings,
    clean_name,
    load_streams_settings,
)


# The numeric settings, with the default a missing or bad value produces.
FLOAT_SETTINGS = [
    ("STREAMS_POLL_SECONDS", "poll_seconds", 5.0),
    ("STREAMS_RECONNECT_SECONDS", "reconnect_seconds", 5.0),
    ("STREAMS_RETRY_RESET_SECONDS", "retry_reset_seconds", 120.0),
]

INT_SETTINGS = [
    ("STREAMS_CLIP_SECONDS", "clip_seconds", 900),
    ("STREAMS_RETRY_LIMIT", "retry_limit", 2),
]

BAD_FLOATS = ["abc", "0", "-5", "   ", "nan", "inf"]
BAD_INTS = ["abc", "0", "-5", "   ", "1.5"]


def settings_for(**values):
    """Build settings from the given environment values alone."""
    return load_streams_settings(values)


def warnings_naming(caplog, key):
    """Return the warning records whose text names *key*."""
    return [
        record
        for record in caplog.records
        if record.levelname == "WARNING" and key in record.getMessage()
    ]


class TestStreamsSettingsShape:
    """The value is a frozen record with the fields fixed by the ticket."""

    def test_fields_are_in_the_fixed_order(self):
        names = [field.name for field in dataclasses.fields(StreamsSettings)]
        assert names == [
            "list_url",
            "poll_seconds",
            "output_dir",
            "clip_seconds",
            "reconnect_seconds",
            "retry_limit",
            "retry_reset_seconds",
            "ffmpeg_path",
            "disk_warn_gb",
            "disk_stop_gb",
            "log_level",
        ]

    def test_settings_cannot_be_changed(self):
        settings = settings_for()
        with pytest.raises(FrozenInstanceError):
            settings.poll_seconds = 1.0

    def test_an_empty_environment_gives_every_default(self):
        settings = settings_for()

        assert settings.list_url == "http://127.0.0.1:8479/"
        assert settings.poll_seconds == 5.0
        assert settings.output_dir == config_module.DEFAULT_RECORDINGS_DIR
        assert settings.clip_seconds == 900
        assert settings.reconnect_seconds == 5.0
        assert settings.retry_limit == 2
        assert settings.retry_reset_seconds == 120.0
        assert settings.ffmpeg_path == "ffmpeg"
        assert settings.disk_warn_gb == 10.0
        assert settings.disk_stop_gb == 2.0
        assert settings.log_level == "INFO"

    def test_the_module_defaults_match_the_table(self):
        assert DEFAULT_LIST_URL == "http://127.0.0.1:8479/"
        assert DEFAULT_POLL_SECONDS == 5.0
        assert DEFAULT_CLIP_SECONDS == 900
        assert DEFAULT_RECONNECT_SECONDS == 5.0
        assert DEFAULT_RETRY_LIMIT == 2
        assert DEFAULT_RETRY_RESET_SECONDS == 120.0
        assert DEFAULT_FFMPEG_PATH == "ffmpeg"
        assert DEFAULT_DISK_WARN_GB == 10.0
        assert DEFAULT_DISK_STOP_GB == 2.0
        assert DEFAULT_LOG_LEVEL == "INFO"

    def test_the_environment_is_read_when_none_is_given(self, monkeypatch):
        monkeypatch.setenv("STREAMS_POLL_SECONDS", "7.5")
        monkeypatch.setenv("STREAMS_RETRY_LIMIT", "4")

        settings = load_streams_settings()

        assert settings.poll_seconds == 7.5
        assert settings.retry_limit == 4


class TestClipsFolder:
    """RECORDINGS_DIR is read exactly as the automator reads it."""

    def test_the_default_is_the_automator_folder(self):
        settings = settings_for()

        assert settings.output_dir == config_module.DEFAULT_RECORDINGS_DIR
        assert os.path.isabs(settings.output_dir)

    def test_an_absolute_path_is_kept(self):
        settings = settings_for(RECORDINGS_DIR=r"D:\Videos\Recordings")

        assert settings.output_dir == r"D:\Videos\Recordings"

    def test_a_unc_path_is_kept(self):
        settings = settings_for(RECORDINGS_DIR=r"\\server\share\clips")

        assert settings.output_dir == r"\\server\share\clips"

    def test_a_relative_path_warns_once_and_falls_back(self, caplog):
        with caplog.at_level("WARNING"):
            settings = settings_for(RECORDINGS_DIR="Recordings")

        assert settings.output_dir == config_module.DEFAULT_RECORDINGS_DIR
        assert len(warnings_naming(caplog, "RECORDINGS_DIR")) == 1

    def test_an_empty_path_warns_once_and_falls_back(self, caplog):
        with caplog.at_level("WARNING"):
            settings = settings_for(RECORDINGS_DIR="   ")

        assert settings.output_dir == config_module.DEFAULT_RECORDINGS_DIR
        assert len(warnings_naming(caplog, "RECORDINGS_DIR")) == 1


class TestListAddress:
    def test_the_address_is_trimmed_and_kept(self):
        settings = settings_for(STREAMS_URL="  http://127.0.0.1:9000/list  ")

        assert settings.list_url == "http://127.0.0.1:9000/list"

    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_a_blank_address_warns_once_and_falls_back(self, caplog, bad):
        with caplog.at_level("WARNING"):
            settings = settings_for(STREAMS_URL=bad)

        assert settings.list_url == DEFAULT_LIST_URL
        assert len(warnings_naming(caplog, "STREAMS_URL")) == 1


class TestNumericSettings:
    @pytest.mark.parametrize("key, field, default", FLOAT_SETTINGS)
    def test_a_missing_float_uses_the_default(self, key, field, default):
        assert getattr(settings_for(), field) == default

    @pytest.mark.parametrize("key, field, default", FLOAT_SETTINGS)
    def test_a_good_float_is_kept(self, key, field, default):
        assert getattr(settings_for(**{key: "12.25"}), field) == 12.25

    @pytest.mark.parametrize("key, field, default", FLOAT_SETTINGS)
    @pytest.mark.parametrize("bad", BAD_FLOATS)
    def test_a_bad_float_warns_once_and_falls_back(self, caplog, key, field, default, bad):
        with caplog.at_level("WARNING"):
            settings = settings_for(**{key: bad})

        assert getattr(settings, field) == default
        assert len(warnings_naming(caplog, key)) == 1

    @pytest.mark.parametrize("key, field, default", INT_SETTINGS)
    def test_a_missing_integer_uses_the_default(self, key, field, default):
        assert getattr(settings_for(), field) == default

    @pytest.mark.parametrize("key, field, default", INT_SETTINGS)
    def test_a_good_integer_is_kept(self, key, field, default):
        assert getattr(settings_for(**{key: "45"}), field) == 45

    @pytest.mark.parametrize("key, field, default", INT_SETTINGS)
    @pytest.mark.parametrize("bad", BAD_INTS)
    def test_a_bad_integer_warns_once_and_falls_back(self, caplog, key, field, default, bad):
        with caplog.at_level("WARNING"):
            settings = settings_for(**{key: bad})

        assert getattr(settings, field) == default
        assert len(warnings_naming(caplog, key)) == 1


class TestRecorderProgram:
    def test_the_default_is_a_bare_name(self):
        assert settings_for().ffmpeg_path == "ffmpeg"

    def test_a_path_with_a_folder_is_kept(self):
        settings = settings_for(STREAMS_FFMPEG_PATH=r"  C:\tools\ffmpeg.exe  ")

        assert settings.ffmpeg_path == r"C:\tools\ffmpeg.exe"

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_a_blank_path_warns_once_and_falls_back(self, caplog, bad):
        with caplog.at_level("WARNING"):
            settings = settings_for(STREAMS_FFMPEG_PATH=bad)

        assert settings.ffmpeg_path == "ffmpeg"
        assert len(warnings_naming(caplog, "STREAMS_FFMPEG_PATH")) == 1


class TestLogLevel:
    def test_a_good_level_is_kept_and_uppercased(self):
        assert settings_for(LOG_LEVEL=" debug ").log_level == "DEBUG"

    @pytest.mark.parametrize("bad", ["verbose", "", "   ", "TRACE"])
    def test_a_bad_level_warns_once_and_falls_back(self, caplog, bad):
        with caplog.at_level("WARNING"):
            settings = settings_for(LOG_LEVEL=bad)

        assert settings.log_level == "INFO"
        assert len(warnings_naming(caplog, "LOG_LEVEL")) == 1


class TestDiskLevels:
    def test_the_defaults_are_ten_and_two(self):
        settings = settings_for()

        assert settings.disk_warn_gb == 10.0
        assert settings.disk_stop_gb == 2.0

    def test_fractional_levels_are_allowed(self):
        settings = settings_for(STREAMS_DISK_WARN_GB="0.5", STREAMS_DISK_STOP_GB="0.25")

        assert settings.disk_warn_gb == 0.5
        assert settings.disk_stop_gb == 0.25

    def test_a_stop_level_below_the_warning_level_is_kept(self):
        settings = settings_for(STREAMS_DISK_WARN_GB="30", STREAMS_DISK_STOP_GB="7")

        assert settings.disk_warn_gb == 30.0
        assert settings.disk_stop_gb == 7.0

    @pytest.mark.parametrize("warn, stop", [("4", "4"), ("2", "10")])
    def test_a_stop_level_not_below_warns_once_and_resets_both(self, caplog, warn, stop):
        with caplog.at_level("WARNING"):
            settings = settings_for(
                STREAMS_DISK_WARN_GB=warn,
                STREAMS_DISK_STOP_GB=stop,
            )

        assert settings.disk_warn_gb == 10.0
        assert settings.disk_stop_gb == 2.0
        assert len(warnings_naming(caplog, "STREAMS_DISK_STOP_GB")) == 1
        assert len(warnings_naming(caplog, "STREAMS_DISK_WARN_GB")) == 1

    @pytest.mark.parametrize("bad", ["abc", "0", "-5", "   "])
    def test_a_bad_warning_level_falls_back(self, caplog, bad):
        with caplog.at_level("WARNING"):
            settings = settings_for(STREAMS_DISK_WARN_GB=bad)

        assert settings.disk_warn_gb == 10.0
        assert settings.disk_stop_gb == 2.0
        assert len(warnings_naming(caplog, "STREAMS_DISK_WARN_GB")) >= 1

    @pytest.mark.parametrize("bad", ["abc", "0", "-5", "   "])
    def test_a_bad_stop_level_falls_back(self, caplog, bad):
        with caplog.at_level("WARNING"):
            settings = settings_for(STREAMS_DISK_STOP_GB=bad)

        assert settings.disk_stop_gb == 2.0
        assert len(warnings_naming(caplog, "STREAMS_DISK_STOP_GB")) >= 1


class TestCleanNameWorkedExamples:
    """Every row of the PRD's worked-examples table."""

    @pytest.mark.parametrize(
        "display_name, expected",
        [
            ("Alif Naim", "Alif_Naim"),
            ("Bro  Adi", "Bro_Adi"),
            ("a/b:c", "a_b_c"),
            (" ... ", "user"),
            ("con", "con_"),
            ("محمد", "محمد"),
        ],
    )
    def test_the_table_rows(self, display_name, expected):
        assert clean_name(display_name, set()) == expected

    def test_a_case_insensitive_clash_suffixes_the_second_person(self):
        taken = set()

        first = clean_name("Qiqi", taken)
        taken.add(first.lower())
        second = clean_name("qiqi", taken)

        assert first == "Qiqi"
        assert second == "qiqi_2"

    def test_names_are_unique_in_arrival_order(self):
        taken = set()
        results = []
        for display_name in ["Qiqi", "qiqi", "QIQI", "QıQı"]:
            name = clean_name(display_name, taken)
            taken.add(name.lower())
            results.append(name)

        assert results == ["Qiqi", "qiqi_2", "QIQI_3", "QıQı"]

    def test_the_function_does_not_change_the_taken_set(self):
        taken = {"alif_naim"}

        name = clean_name("Alif Naim", taken)

        assert name == "Alif_Naim_2"
        assert taken == {"alif_naim"}


class TestCleanNameRules:
    @pytest.mark.parametrize(
        "display_name, expected",
        [
            ("  Alif Naim  ", "Alif_Naim"),
            ("Alif\tNaim", "Alif_Naim"),
            ("Alif \t Naim", "Alif_Naim"),
            (r'a\b/c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j"),
            ("a\x00b", "a_b"),
            ("a\x1fb", "a_b"),
            ("a\x7fb", "a_b"),
            ("a___b", "a_b"),
            ("__a__b__", "a_b"),
            ("report.", "report"),
            ("report.. .", "report"),
            ("   ", "user"),
            ("", "user"),
            ("\t\t", "user"),
            ("...", "user"),
        ],
    )
    def test_the_character_rules(self, display_name, expected):
        assert clean_name(display_name, set()) == expected

    def test_a_long_name_is_cut_to_the_limit(self):
        name = clean_name("a" * 80, set())

        assert name == "a" * NAME_MAX_LENGTH
        assert NAME_MAX_LENGTH == 40

    def test_the_cut_never_leaves_a_trailing_underscore_or_dot(self):
        cut_underscore = clean_name("a" * 39 + " b", set())
        cut_dot = clean_name("a" * 39 + ".b", set())

        assert cut_underscore == "a" * 39
        assert cut_dot == "a" * 39

    @pytest.mark.parametrize(
        "display_name",
        [
            "Alif Naim",
            "Bro  Adi",
            "a/b:c",
            " ... ",
            "con",
            "محمد",
            "user",
            "con_",
            "a" * 39 + " b",
            "a" * 80,
        ],
    )
    def test_cleaning_an_already_clean_name_changes_nothing(self, display_name):
        once = clean_name(display_name, set())

        assert clean_name(once, set()) == once

    def test_a_suffixed_name_is_stable_when_cleaned_again(self):
        once = clean_name("Qiqi", {"qiqi"})

        assert once == "Qiqi_2"
        assert clean_name(once, set()) == once

    @pytest.mark.parametrize(
        "device_name",
        [
            "CON",
            "PRN",
            "AUX",
            "NUL",
            "COM1",
            "COM5",
            "COM9",
            "LPT1",
            "LPT5",
            "LPT9",
        ],
    )
    def test_device_names_get_a_trailing_underscore(self, device_name):
        assert clean_name(device_name.lower(), set()) == device_name.lower() + "_"
        assert clean_name(device_name.upper(), set()) == device_name.upper() + "_"

    @pytest.mark.parametrize(
        "harmless_name",
        ["console", "com0", "com10", "lpt0", "null", "com1x", "my con"],
    )
    def test_names_that_merely_look_like_devices_are_kept(self, harmless_name):
        expected = harmless_name.replace(" ", "_")
        assert clean_name(harmless_name, set()) == expected

    def test_a_device_name_that_is_already_taken_is_suffixed(self):
        name = clean_name("con", {"con_"})

        assert name == "con_2"

    def test_a_bare_taken_device_word_does_not_block_the_safe_name(self):
        assert clean_name("con", {"con"}) == "con_"

    def test_the_trailing_underscore_does_not_break_uniqueness(self):
        taken = set()
        first = clean_name("con", taken)
        taken.add(first.lower())
        second = clean_name("CON", taken)

        assert first == "con_"
        assert second == "CON_2"
