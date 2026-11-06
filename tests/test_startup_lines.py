"""Tests for the launch lines the List Recorder work adds.

Two location lines go right after the DPI line and immediately before the
existing ``Recording mode: ...`` line, on a real run and a rehearsal alike: the
Telegram program and the Clips folder. One switch line follows the broadcast
startup line, before the rehearsal branch, and it appears on every launch
including a rehearsal.

``--check-obs`` reports on broadcasting only: its six lines stay six and none
of the lines below appears there.
"""
import datetime
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main


_MONDAY = datetime.datetime(2024, 1, 1)


def _make_config(
    telegram_path=r"D:\Apps\Telegram Desktop\Telegram.exe",
    recordings_dir=r"D:\Recordings",
    record_list_enabled=False,
    broadcast_enabled=False,
):
    """A Config-like MagicMock holding only the startup-line values."""
    cfg = MagicMock()
    cfg.telegram_path = telegram_path
    cfg.recordings_dir = recordings_dir
    cfg.record_list_enabled = record_list_enabled
    cfg.recording_mode = "off"
    cfg.broadcast_enabled = broadcast_enabled
    cfg.broadcast_obs_host = "127.0.0.1"
    cfg.broadcast_obs_port = 4455
    cfg.broadcast_obs_password = ""
    cfg.broadcast_obs_path = r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"
    return cfg


def _main_messages(caplog):
    """Return the messages the automator's own logger wrote, in order."""
    return [record.getMessage() for record in caplog.records if record.name == "main"]


# ---------------------------------------------------------------------------
# The two location lines
# ---------------------------------------------------------------------------

class TestLocationLines:
    """One line for the Telegram program, one for the Clips folder."""

    def test_the_program_line_names_the_resolved_path(self, caplog):
        cfg = _make_config(telegram_path=r"D:\Apps\Telegram Desktop\Telegram.exe")
        with patch.object(main, "config", cfg), caplog.at_level(logging.INFO):
            main.log_startup_locations()

        messages = _main_messages(caplog)
        assert messages[0] == r"Telegram program: D:\Apps\Telegram Desktop\Telegram.exe"

    def test_the_clips_line_carries_the_parenthetical(self, caplog):
        cfg = _make_config(recordings_dir=r"D:\Recordings")
        with patch.object(main, "config", cfg), caplog.at_level(logging.INFO):
            main.log_startup_locations()

        messages = _main_messages(caplog)
        assert messages[1] == (
            r"Clips folder: D:\Recordings (each Listed Person records into a "
            r"folder of their own inside it)"
        )

    def test_the_program_line_comes_first_and_only_the_two_are_written(self, caplog):
        cfg = _make_config()
        with patch.object(main, "config", cfg), caplog.at_level(logging.INFO):
            main.log_startup_locations()

        messages = _main_messages(caplog)
        assert len(messages) == 2
        assert messages[0].startswith("Telegram program: ")
        assert messages[1].startswith("Clips folder: ")


# ---------------------------------------------------------------------------
# The switch line
# ---------------------------------------------------------------------------

class TestSwitchLine:
    """'List Recorder switch: on.' or 'off.', and nothing else."""

    def test_on(self, caplog):
        cfg = _make_config(record_list_enabled=True)
        with patch.object(main, "config", cfg), caplog.at_level(logging.INFO):
            main.log_list_recorder_startup()

        assert _main_messages(caplog) == ["List Recorder switch: on."]

    def test_off(self, caplog):
        cfg = _make_config(record_list_enabled=False)
        with patch.object(main, "config", cfg), caplog.at_level(logging.INFO):
            main.log_list_recorder_startup()

        assert _main_messages(caplog) == ["List Recorder switch: off."]


# ---------------------------------------------------------------------------
# Where the lines sit in a launch
# ---------------------------------------------------------------------------

def _patched_launch(argv, config=None, **overrides):
    """Return ``(mocks, patch_context)`` for one launch of the program."""
    if config is None:
        config = _make_config()

    values = dict(
        config=config,
        sys=MagicMock(argv=argv),
        validate_schedule_at_startup=MagicMock(return_value=None),
        set_dpi_awareness=MagicMock(),
        log_recording_startup=MagicMock(
            side_effect=lambda *a, **k: main.logger.info("Recording mode: off.")
        ),
        prepare_broadcast=MagicMock(
            side_effect=lambda *a, **k: main.logger.info("Broadcast switch: off.")
        ),
        prepare_list_recorder=MagicMock(),
        run_dry_run=MagicMock(),
        run_monitor_cycle=MagicMock(side_effect=KeyboardInterrupt),
        stop_list_recorder=MagicMock(),
        stop_recording=MagicMock(),
        stop_broadcast=MagicMock(),
        time=MagicMock(),
    )
    values.update(overrides)
    return values, patch.multiple("main", **values)


class TestLaunchOrder:
    """The location lines and the switch line sit where the spec puts them."""

    def test_a_real_run_writes_the_lines_in_order(self, caplog):
        mocks, ctx = _patched_launch(["main.py"])
        with ctx, caplog.at_level(logging.INFO):
            main.main()

        messages = _main_messages(caplog)
        assert messages == [
            "Starting Telegram Monitor Service...",
            r"Telegram program: D:\Apps\Telegram Desktop\Telegram.exe",
            r"Clips folder: D:\Recordings (each Listed Person records into a "
            r"folder of their own inside it)",
            "Recording mode: off.",
            "Broadcast switch: off.",
            "List Recorder switch: off.",
            "Monitor Service stopped by user.",
        ]

    def test_the_location_lines_sit_right_after_the_dpi_line(self):
        calls = []
        mocks, ctx = _patched_launch(
            ["main.py"],
            set_dpi_awareness=MagicMock(side_effect=lambda: calls.append("dpi")),
            log_startup_locations=MagicMock(side_effect=lambda: calls.append("locations")),
            log_recording_startup=MagicMock(side_effect=lambda: calls.append("recording")),
        )
        with ctx:
            main.main()

        assert calls == ["dpi", "locations", "recording"]

    def test_a_rehearsal_writes_the_same_lines(self, caplog):
        mocks, ctx = _patched_launch(["main.py", "--dry-run"])
        with ctx, caplog.at_level(logging.INFO):
            main.main()

        messages = _main_messages(caplog)
        assert r"Telegram program: D:\Apps\Telegram Desktop\Telegram.exe" in messages
        assert any(message.startswith(r"Clips folder: D:\Recordings") for message in messages)
        assert "List Recorder switch: off." in messages
        mocks["run_dry_run"].assert_called_once()

    def test_a_rehearsal_still_says_the_switch_is_on(self, caplog):
        mocks, ctx = _patched_launch(
            ["main.py", "--dry-run"], config=_make_config(record_list_enabled=True)
        )
        with ctx, caplog.at_level(logging.INFO):
            main.main()

        assert "List Recorder switch: on." in _main_messages(caplog)

    def test_the_switch_line_follows_the_broadcast_line(self, caplog):
        mocks, ctx = _patched_launch(["main.py"])
        with ctx, caplog.at_level(logging.INFO):
            main.main()

        messages = _main_messages(caplog)
        assert messages.index("Broadcast switch: off.") + 1 == messages.index(
            "List Recorder switch: off."
        )


# ---------------------------------------------------------------------------
# --check-obs
# ---------------------------------------------------------------------------

_OBS_LINES = [
    "OBS program: found.",
    "OBS password: not set.",
    "OBS is running: no.",
    "OBS stream: not streaming.",
]


class TestCheckObsIsUntouched:
    """Six lines about broadcasting, and none of the new launch lines."""

    def test_six_lines_and_none_of_the_new_ones(self, caplog):
        clock = MagicMock()
        clock.datetime.now.return_value = _MONDAY + datetime.timedelta(hours=12)
        mocks, ctx = _patched_launch(
            ["main.py", "--check-obs"],
            datetime=clock,
            check_report=MagicMock(return_value=(list(_OBS_LINES), True)),
        )
        with ctx, caplog.at_level(logging.INFO):
            with pytest.raises(SystemExit) as stopped:
                main.main()

        messages = _main_messages(caplog)
        assert stopped.value.code == 0
        assert messages[0] == "Starting Telegram Monitor Service..."
        assert len(messages[1:]) == 6
        assert not [m for m in messages if m.startswith("Telegram program: ")]
        assert not [m for m in messages if m.startswith("Clips folder: ")]
        assert not [m for m in messages if m.startswith("List Recorder switch: ")]

    def test_nothing_of_the_launch_ran(self):
        mocks, ctx = _patched_launch(["main.py", "--check-obs"])
        with ctx:
            with pytest.raises(SystemExit):
                main.main()

        mocks["set_dpi_awareness"].assert_not_called()
        mocks["log_recording_startup"].assert_not_called()
        mocks["prepare_broadcast"].assert_not_called()
        mocks["prepare_list_recorder"].assert_not_called()
