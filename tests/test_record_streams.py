"""Tests for the List Recorder program.

The farm is driven with fake recorder engines, fake free space, and hand-written
Stream List entries: no recorder program is spawned, no stream is opened, no
network request is made, nothing is read from a real disk, and nothing sleeps.
``main`` is exercised through patched boundaries so its exit codes and its
Ctrl+C path can be checked without running the loop for real.
"""
import datetime
import logging
import sys
from pathlib import Path

import pytest

# Add the parent directory to sys.path so we can import record_streams
sys.path.insert(0, str(Path(__file__).parent.parent))

import record_streams
from record_streams import ListRecorderFarm
from recorder import (
    STATUS_DISK_STOP,
    STATUS_GAVE_UP,
    STATUS_RUNNING,
    STATUS_STOPPED,
    STATUS_WAITING,
)
from stream_list import ListedStream
from streams_config import StreamsSettings

GB = 1024 ** 3

# 2024-01-01 is a Monday; moments are offsets from it, on the local clock.
_MONDAY = datetime.datetime(2024, 1, 1)


def at(seconds: int = 0) -> datetime.datetime:
    """A fixed moment used to drive the cycles."""
    return _MONDAY + datetime.timedelta(seconds=seconds)


def listed(
    name: str, address: str = "http://bridge.test/1/camera.m3u8"
) -> ListedStream:
    """One hand-written Stream List entry."""
    return ListedStream(username=name, stream=address)


def make_settings(**overrides) -> StreamsSettings:
    """Settings with the PRD defaults, overridable per test."""
    values = dict(
        list_url="http://127.0.0.1:8479/",
        poll_seconds=5.0,
        output_dir=r"D:\Clips",
        clip_seconds=900,
        reconnect_seconds=5.0,
        retry_limit=2,
        retry_reset_seconds=120.0,
        ffmpeg_path="ffmpeg",
        disk_warn_gb=10.0,
        disk_stop_gb=2.0,
        log_level="INFO",
    )
    values.update(overrides)
    return StreamsSettings(**values)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeRecorder:
    """A recorder stand-in that remembers every call the farm makes.

    ``start_ok`` decides what a start does. ``idle_status`` is what is
    reported while no child is running, so a test can put the engine in the
    state it wants: stopped, waiting-to-retry, gave-up, or disk-stop.
    """

    def __init__(self, settings, start_ok=True):
        self.settings = settings
        self.start_ok = start_ok
        self.running = False
        self.idle_status = STATUS_STOPPED
        self.starts = []
        self.stops = 0
        self.disk_stops = 0
        self.supervisions = []

    def start(self, moment):
        self.starts.append(moment)
        if self.start_ok:
            self.running = True
            return True
        self.running = False
        self.idle_status = STATUS_STOPPED
        return False

    def stop(self):
        self.stops += 1
        self.running = False
        self.idle_status = STATUS_STOPPED
        return True

    def stop_for_disk(self):
        self.disk_stops += 1
        self.running = False
        self.idle_status = STATUS_DISK_STOP
        return True

    def supervise(self, moment):
        self.supervisions.append(moment)

    def is_running(self):
        return self.running

    def status(self):
        if self.running:
            return STATUS_RUNNING
        return self.idle_status


class FakeRecorderFactory:
    """Builds :class:`FakeRecorder` engines and keeps the ones it made."""

    def __init__(self, start_ok=True):
        self.start_ok = start_ok
        self.made = []

    def __call__(self, settings):
        engine = FakeRecorder(settings, self.start_ok)
        self.made.append(engine)
        return engine


class FakeFreeSpace:
    """Answers free bytes for the Clips folder, and remembers it was asked."""

    def __init__(self, gigabytes=50.0):
        self.gigabytes = gigabytes
        self.reads = 0

    def __call__(self, folder):
        self.reads += 1
        return int(self.gigabytes * GB)


def build_farm(free_gb=50.0, start_ok=True, settings=None):
    """Return ``(farm, factory, free_space)`` wired with fakes."""
    settings = settings or make_settings()
    factory = FakeRecorderFactory(start_ok)
    free = FakeFreeSpace(free_gb)
    farm = ListRecorderFarm(
        settings, recorder_factory=factory, free_space_bytes=free
    )
    return farm, factory, free


def messages(caplog):
    """Every captured log message, oldest first."""
    return [record.getMessage() for record in caplog.records]


def count_lines(caplog):
    """The running-count lines only, so other 'recording' lines do not count."""
    markers = (
        "person is recording",
        "people are recording",
        "List Recordings are running",
    )
    return [
        text for text in messages(caplog) if any(marker in text for marker in markers)
    ]


# ---------------------------------------------------------------------------
# The shape of the farm and what it hands the engine
# ---------------------------------------------------------------------------

class TestEngineSettings:
    """Each person gets their own engine, led by their Clean Name."""

    def test_the_farm_has_the_documented_surface(self):
        farm, _factory, _free = build_farm()

        assert callable(farm.cycle)
        assert callable(farm.stop_all)
        assert callable(record_streams.main)

    def test_the_engine_gets_the_clean_name_prefix_and_the_settings(self):
        farm, factory, _free = build_farm()

        farm.cycle(
            at(0), [listed("Alif Naim", "http://bridge.test/8198214155/camera.m3u8")]
        )

        engine_settings = factory.made[0].settings
        assert engine_settings.clip_name_prefix == "Alif_Naim_"
        assert engine_settings.stream_url == "http://bridge.test/8198214155/camera.m3u8"
        assert engine_settings.output_dir == r"D:\Clips"
        assert engine_settings.clip_seconds == 900
        assert engine_settings.reconnect_seconds == 5.0
        assert engine_settings.ffmpeg_path == "ffmpeg"
        assert engine_settings.retry_limit == 2
        assert engine_settings.retry_reset_seconds == 120.0

    def test_the_free_space_read_asks_for_the_clips_folder(self):
        calls = []
        farm = ListRecorderFarm(
            make_settings(output_dir=r"D:\Clips"),
            recorder_factory=FakeRecorderFactory(),
            free_space_bytes=lambda folder: calls.append(folder) or 50 * GB,
        )

        farm.cycle(at(0), [])

        assert calls == [r"D:\Clips"]

    def test_two_display_names_that_clean_the_same_get_different_prefixes(self):
        farm, factory, _free = build_farm()

        farm.cycle(at(0), [listed("a b"), listed("a_b")])

        prefixes = [engine.settings.clip_name_prefix for engine in factory.made]
        assert prefixes == ["a_b_", "a_b_2_"]

    def test_one_engine_is_made_per_person(self):
        farm, factory, _free = build_farm()

        farm.cycle(at(0), [listed("Jevv"), listed("noww")])

        assert len(factory.made) == 2
        assert [engine.settings.clip_name_prefix for engine in factory.made] == [
            "Jevv_",
            "noww_",
        ]

    def test_the_default_engine_is_the_real_recorder(self, monkeypatch):
        made = []

        class RecordingEngine(FakeRecorder):
            def __init__(self, settings):
                super().__init__(settings)
                made.append(self)

        # The default factory is looked up when the farm is built, so replacing
        # the module value keeps the real program out of the test.
        monkeypatch.setattr(record_streams, "Recorder", RecordingEngine)
        farm = ListRecorderFarm(make_settings(), free_space_bytes=FakeFreeSpace())

        farm.cycle(at(0), [listed("Jevv")])

        assert len(made) == 1
        assert made[0].settings.clip_name_prefix == "Jevv_"


# ---------------------------------------------------------------------------
# People appearing and staying
# ---------------------------------------------------------------------------

class TestStartingPeople:
    """A new person is started once; a listed person is left alone."""

    def test_a_new_person_is_started_once(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, _free = build_farm()

        farm.cycle(at(0), [listed("Jevv")])

        assert len(factory.made) == 1
        assert factory.made[0].starts == [at(0)]
        assert any(
            "List Recording started for Jevv" in text for text in messages(caplog)
        )

    def test_a_person_still_listed_is_not_restarted(self):
        farm, factory, _free = build_farm()

        farm.cycle(at(0), [listed("Jevv")])
        farm.cycle(at(5), [listed("Jevv")])
        farm.cycle(at(10), [listed("Jevv")])

        assert len(factory.made) == 1
        assert factory.made[0].starts == [at(0)]


# ---------------------------------------------------------------------------
# Absence Grace
# ---------------------------------------------------------------------------

class TestAbsenceGrace:
    """One missing reply is not proof; two in a row are."""

    def test_one_missing_reply_keeps_the_recording(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, _free = build_farm()

        farm.cycle(at(0), [listed("Jevv")])
        engine = factory.made[0]
        farm.cycle(at(5), [])

        assert engine.stops == 0
        assert engine.is_running()
        assert any(
            record.levelno == logging.DEBUG
            and "missing from the Stream List once" in record.getMessage()
            for record in caplog.records
        )

    def test_two_missing_replies_stop_the_recording(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, _free = build_farm()

        farm.cycle(at(0), [listed("Jevv")])
        engine = factory.made[0]
        farm.cycle(at(5), [])
        farm.cycle(at(10), [])

        assert engine.stops == 1
        assert not engine.is_running()
        assert any("left the Stream List" in text for text in messages(caplog))

    def test_a_return_after_two_misses_starts_a_fresh_recording(self):
        farm, factory, _free = build_farm()

        farm.cycle(at(0), [listed("Jevv")])
        farm.cycle(at(5), [])
        farm.cycle(at(10), [])
        farm.cycle(at(15), [listed("Jevv")])

        assert len(factory.made) == 2
        assert factory.made[1].starts == [at(15)]
        assert factory.made[1].settings.clip_name_prefix == "Jevv_"

    def test_a_miss_is_forgotten_when_the_person_comes_back(self):
        farm, factory, _free = build_farm()

        farm.cycle(at(0), [listed("Jevv")])
        farm.cycle(at(5), [])
        farm.cycle(at(10), [listed("Jevv")])
        farm.cycle(at(15), [])

        engine = factory.made[0]
        assert engine.stops == 0
        assert engine.is_running()
        assert len(factory.made) == 1


# ---------------------------------------------------------------------------
# A changed stream address
# ---------------------------------------------------------------------------

class TestAddressChange:
    """A new address means a fresh child, with its counters cleared."""

    def test_a_changed_address_stops_the_old_recorder_and_starts_a_new_one(
        self, caplog
    ):
        caplog.set_level(logging.DEBUG)
        farm, factory, _free = build_farm()

        farm.cycle(at(0), [listed("Jevv", "http://bridge.test/1/camera.m3u8")])
        old = factory.made[0]
        farm.cycle(at(5), [listed("Jevv", "http://bridge.test/2/camera.m3u8")])
        new = factory.made[1]

        assert old.stops == 1
        assert not old.is_running()
        assert new.starts == [at(5)]
        assert new.settings.stream_url == "http://bridge.test/2/camera.m3u8"
        assert new.settings.clip_name_prefix == "Jevv_"
        assert any(
            "stream address for Jevv changed" in text for text in messages(caplog)
        )

    def test_a_changed_address_starts_at_once_and_resets_the_wait(self):
        farm, factory, _free = build_farm(start_ok=False)

        farm.cycle(at(0), [listed("Jevv", "http://bridge.test/1/camera.m3u8")])
        factory.start_ok = True
        farm.cycle(at(1), [listed("Jevv", "http://bridge.test/2/camera.m3u8")])

        new = factory.made[1]
        assert new.starts == [at(1)]
        assert new.is_running()

    def test_a_given_up_person_with_a_new_address_is_treated_as_new(self):
        farm, factory, _free = build_farm(start_ok=False)

        farm.cycle(at(0), [listed("Jevv", "http://bridge.test/1/camera.m3u8")])
        farm.cycle(at(5), [listed("Jevv", "http://bridge.test/1/camera.m3u8")])
        factory.start_ok = True
        farm.cycle(at(10), [listed("Jevv", "http://bridge.test/2/camera.m3u8")])

        new = factory.made[1]
        assert new.starts == [at(10)]
        assert new.is_running()


# ---------------------------------------------------------------------------
# Given Up
# ---------------------------------------------------------------------------

class TestGivenUp:
    """An exhausted Retry Limit is quiet while the person stays listed."""

    def _give_up(self, farm, factory):
        """Drive one person to Given Up through two failed first starts."""
        farm.cycle(at(0), [listed("Jevv")])
        farm.cycle(at(5), [listed("Jevv")])
        return factory.made[0]

    def test_the_retry_limit_gives_up_with_one_warning(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, _free = build_farm(start_ok=False)

        engine = self._give_up(farm, factory)

        assert engine.starts == [at(0), at(5)]
        give_up = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "giving up while they stay on the Stream List" in record.getMessage()
        ]
        assert len(give_up) == 1
        assert "Jevv" in give_up[0]

    def test_a_given_up_person_gets_no_further_attempts_while_listed(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, _free = build_farm(start_ok=False)
        engine = self._give_up(farm, factory)
        caplog.clear()

        farm.cycle(at(20), [listed("Jevv")])
        farm.cycle(at(25), [listed("Jevv")])

        assert engine.starts == [at(0), at(5)]
        assert engine.supervisions == [at(0), at(5)]
        assert messages(caplog) == []

    def test_a_given_up_person_leaving_and_returning_starts_again(self):
        farm, factory, _free = build_farm(start_ok=False)
        self._give_up(farm, factory)

        farm.cycle(at(10), [])
        farm.cycle(at(15), [])
        factory.start_ok = True
        farm.cycle(at(20), [listed("Jevv")])

        assert len(factory.made) == 2
        assert factory.made[1].starts == [at(20)]
        assert factory.made[1].is_running()

    def test_the_engine_giving_up_marks_the_person_without_extra_noise(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, _free = build_farm()
        farm.cycle(at(0), [listed("Jevv")])
        engine = factory.made[0]

        # The engine reconnected twice and reported gave-up; its own line was
        # written in the engine, so the farm only notes it at debug.
        engine.running = False
        engine.idle_status = STATUS_GAVE_UP
        caplog.clear()
        farm.cycle(at(5), [listed("Jevv")])

        assert engine.supervisions == [at(0), at(5)]
        assert not any(
            record.levelno >= logging.INFO and "Jevv" in record.getMessage()
            for record in caplog.records
        )

        caplog.clear()
        farm.cycle(at(10), [listed("Jevv")])
        assert engine.supervisions == [at(0), at(5)]
        assert messages(caplog) == []


# ---------------------------------------------------------------------------
# A first start that failed is the farm's own retry
# ---------------------------------------------------------------------------

class TestFirstStartRetries:
    """Nothing was ever started, so the engine cannot reconnect it."""

    def test_a_failed_first_start_is_retried_after_the_wait(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, _free = build_farm(start_ok=False)
        farm.cycle(at(0), [listed("Jevv")])
        engine = factory.made[0]

        assert engine.starts == [at(0)]
        assert any(
            record.levelno == logging.WARNING and "Jevv" in record.getMessage()
            for record in caplog.records
        )

        # Not yet: the reconnect wait is five seconds.
        farm.cycle(at(4), [listed("Jevv")])
        assert engine.starts == [at(0)]

        engine.start_ok = True
        farm.cycle(at(5), [listed("Jevv")])
        assert engine.starts == [at(0), at(5)]
        assert engine.is_running()

    def test_a_recorder_waiting_to_reconnect_is_left_to_the_engine(self):
        farm, factory, _free = build_farm()
        farm.cycle(at(0), [listed("Jevv")])
        engine = factory.made[0]

        # The child dropped; the engine is waiting out its own reconnect delay.
        engine.running = False
        engine.idle_status = STATUS_WAITING
        farm.cycle(at(5), [listed("Jevv")])

        assert engine.starts == [at(0)]
        assert engine.supervisions == [at(0), at(5)]

    def test_a_first_start_that_never_works_costs_only_the_limit(self):
        farm, factory, _free = build_farm(start_ok=False)
        farm.cycle(at(0), [listed("Jevv")])
        engine = factory.made[0]

        for moment in (5, 10, 15, 20):
            farm.cycle(at(moment), [listed("Jevv")])

        # Two attempts only: the limit is two, and Given Up holds after that.
        assert engine.starts == [at(0), at(5)]


# ---------------------------------------------------------------------------
# A reply that cannot be read
# ---------------------------------------------------------------------------

class TestUnreadableReply:
    """An unreadable reply stops nothing and forgets nobody."""

    def test_none_stops_nothing_and_forgets_nobody(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, _free = build_farm()
        farm.cycle(at(0), [listed("Jevv")])
        engine = factory.made[0]

        farm.cycle(at(5), None)

        assert engine.stops == 0
        assert engine.is_running()
        assert len(factory.made) == 1
        assert engine.supervisions == [at(0), at(5)]

    def test_none_on_the_first_reply_is_harmless(self):
        farm, factory, _free = build_farm()

        farm.cycle(at(0), None)

        assert factory.made == []

    def test_none_does_not_count_as_a_missed_reply(self):
        farm, factory, _free = build_farm()
        farm.cycle(at(0), [listed("Jevv")])
        engine = factory.made[0]

        farm.cycle(at(5), None)
        farm.cycle(at(10), [])

        # The miss counter saw only the second reply, so the person stays.
        assert engine.stops == 0
        assert engine.is_running()

    def test_warning_once_then_debug_then_information_on_recovery(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, _factory, _free = build_farm()

        farm.cycle(at(0), None)
        farm.cycle(at(5), None)
        farm.cycle(at(10), [], )
        farm.cycle(at(15), [])

        warnings = [
            record for record in caplog.records if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "could not be read" in warnings[0].getMessage()

        debug_repeats = [
            record
            for record in caplog.records
            if record.levelno == logging.DEBUG
            and "could not be read" in record.getMessage()
        ]
        assert len(debug_repeats) == 1

        recovered = [
            record
            for record in caplog.records
            if record.levelno == logging.INFO
            and "answering again" in record.getMessage()
        ]
        assert len(recovered) == 1

    def test_a_long_outage_warns_again_every_tenth_reply(self, caplog):
        # The PRD's failure table asks for one warning per ten consecutive
        # unreadable replies, so an outage lasting minutes is still visible.
        caplog.set_level(logging.WARNING)
        farm, _factory, _free = build_farm()

        for number in range(10):
            farm.cycle(at(number * 5), None)

        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "could not be read" in record.getMessage()
        ]
        assert len(warnings) == 2
        assert "10 times in a row" in warnings[1].getMessage()


# ---------------------------------------------------------------------------
# The disk guard
# ---------------------------------------------------------------------------

class TestDiskGuard:
    """Low space warns once; critically low space stops everything once."""

    def test_entering_the_stop_level_stops_everything_through_stop_for_disk(
        self, caplog
    ):
        caplog.set_level(logging.DEBUG)
        farm, factory, free = build_farm()
        farm.cycle(at(0), [listed("Jevv"), listed("noww")])
        first, second = factory.made

        free.gigabytes = 1.0
        farm.cycle(at(5), [listed("Jevv"), listed("noww")])

        assert first.disk_stops == 1
        assert second.disk_stops == 1
        assert first.stops == 0
        assert second.stops == 0
        assert any(
            record.levelno == logging.ERROR
            and "critically low" in record.getMessage()
            for record in caplog.records
        )

    def test_the_latch_stops_everything_once_and_keeps_the_people(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, free = build_farm()
        farm.cycle(at(0), [listed("Jevv")])
        engine = factory.made[0]

        free.gigabytes = 1.0
        farm.cycle(at(5), [listed("Jevv")])
        caplog.clear()
        farm.cycle(at(10), [listed("Jevv")])
        farm.cycle(at(15), [listed("Jevv")])

        assert engine.disk_stops == 1
        assert messages(caplog) == []

    def test_the_latch_blocks_new_starts(self):
        farm, factory, free = build_farm()
        farm.cycle(at(0), [listed("Jevv")])
        free.gigabytes = 1.0
        farm.cycle(at(5), [listed("Jevv")])

        farm.cycle(at(10), [listed("Jevv"), listed("noww")])

        assert len(factory.made) == 1

    def test_space_back_at_the_warning_level_resumes_everyone(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, free = build_farm()
        farm.cycle(at(0), [listed("Jevv"), listed("noww")])
        free.gigabytes = 1.0
        farm.cycle(at(5), [listed("Jevv"), listed("noww")])

        free.gigabytes = 50.0
        farm.cycle(at(10), [listed("Jevv"), listed("noww")])

        for engine in factory.made:
            assert engine.starts == [at(0), at(10)]
            assert engine.is_running()
        assert any(
            record.levelno == logging.INFO
            and "resuming List Recordings" in record.getMessage()
            for record in caplog.records
        )

    def test_recovery_gives_a_given_up_person_a_fresh_run(self):
        farm, factory, free = build_farm(start_ok=False)
        farm.cycle(at(0), [listed("Jevv")])
        farm.cycle(at(5), [listed("Jevv")])
        engine = factory.made[0]

        free.gigabytes = 1.0
        farm.cycle(at(10), [listed("Jevv")])

        free.gigabytes = 50.0
        engine.start_ok = True
        farm.cycle(at(15), [listed("Jevv")])

        assert engine.starts == [at(0), at(5), at(15)]
        assert engine.is_running()

    def test_the_low_space_warning_is_one_per_crossing(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, _factory, free = build_farm()

        free.gigabytes = 5.0
        farm.cycle(at(0), [])
        farm.cycle(at(5), [])
        low_space = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "Low disk space" in record.getMessage()
        ]
        assert len(low_space) == 1

        free.gigabytes = 50.0
        farm.cycle(at(10), [])
        free.gigabytes = 5.0
        farm.cycle(at(15), [])

        low_space = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "Low disk space" in record.getMessage()
        ]
        assert len(low_space) == 2

    def test_a_recording_keeps_running_at_the_warning_level(self):
        farm, factory, free = build_farm()
        free.gigabytes = 5.0

        farm.cycle(at(0), [listed("Jevv")])
        farm.cycle(at(5), [listed("Jevv")])

        engine = factory.made[0]
        assert engine.starts == [at(0)]
        assert engine.is_running()


# ---------------------------------------------------------------------------
# One fault never ends the program
# ---------------------------------------------------------------------------

class TestFaults:
    """A broken boundary is logged and the next cycle carries on."""

    def test_a_broken_free_space_read_never_ends_the_cycle(self, caplog):
        caplog.set_level(logging.ERROR)
        reads = []

        def flaky_free_space(folder):
            reads.append(folder)
            if len(reads) == 1:
                raise OSError("the disk could not be read")
            return 50 * GB

        factory = FakeRecorderFactory()
        farm = ListRecorderFarm(
            make_settings(),
            recorder_factory=factory,
            free_space_bytes=flaky_free_space,
        )

        farm.cycle(at(0), [listed("Jevv")])
        assert factory.made == []
        assert any(
            record.levelno == logging.ERROR for record in caplog.records
        )

        farm.cycle(at(5), [listed("Jevv")])
        assert len(factory.made) == 1
        assert factory.made[0].is_running()

    def test_a_start_that_throws_does_not_disturb_another_person(self, caplog):
        caplog.set_level(logging.ERROR)

        class ExplodingRecorder(FakeRecorder):
            def start(self, moment):
                self.starts.append(moment)
                raise RuntimeError("the engine broke")

        class OneBrokenFactory(FakeRecorderFactory):
            def __call__(self, settings):
                engine_type = (
                    ExplodingRecorder
                    if settings.clip_name_prefix == "Jevv_"
                    else FakeRecorder
                )
                engine = engine_type(settings)
                self.made.append(engine)
                return engine

        factory = OneBrokenFactory()
        farm = ListRecorderFarm(
            make_settings(),
            recorder_factory=factory,
            free_space_bytes=FakeFreeSpace(),
        )

        farm.cycle(at(0), [listed("Jevv"), listed("noww")])

        broken, healthy = factory.made
        assert broken.starts == [at(0)]
        assert healthy.starts == [at(0)]
        assert healthy.is_running()
        assert any(
            record.levelno == logging.ERROR and "Jevv" in record.getMessage()
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# Stopping everything
# ---------------------------------------------------------------------------

class TestStopAll:
    """No child survives the program."""

    def test_stop_all_stops_every_child(self):
        farm, factory, _free = build_farm()
        farm.cycle(at(0), [listed("Jevv"), listed("noww")])

        farm.stop_all()

        assert [engine.stops for engine in factory.made] == [1, 1]
        assert not any(engine.is_running() for engine in factory.made)

    def test_stop_all_keeps_going_when_one_child_throws(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, factory, _free = build_farm()
        farm.cycle(at(0), [listed("Jevv"), listed("noww")])
        broken, healthy = factory.made

        def explode():
            raise RuntimeError("boom")

        broken.stop = explode
        farm.stop_all()

        assert broken.stops == 0
        assert healthy.stops == 1
        assert any(
            record.levelno == logging.ERROR and "Jevv" in record.getMessage()
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# The running count line
# ---------------------------------------------------------------------------

class TestRunningCount:
    """One information line, and only when the number changes."""

    def test_the_count_line_appears_only_when_the_count_changes(self, caplog):
        caplog.set_level(logging.DEBUG)
        farm, _factory, _free = build_farm()

        farm.cycle(at(0), [listed("Jevv")])
        farm.cycle(at(5), [listed("Jevv")])
        farm.cycle(at(10), [listed("Jevv"), listed("noww")])
        farm.cycle(at(15), [listed("Jevv"), listed("noww")])
        farm.cycle(at(20), [])
        farm.cycle(at(25), [])

        assert count_lines(caplog) == [
            "One person is recording.",
            "2 people are recording.",
            "No List Recordings are running now.",
        ]


# ---------------------------------------------------------------------------
# Reading a reply
# ---------------------------------------------------------------------------

class TestReadingTheReply:
    """The list client is faked; only the reading rules are exercised."""

    def test_the_request_timeout_follows_the_poll_interval(self):
        assert record_streams.list_request_timeout(5.0) == 5.0
        assert record_streams.list_request_timeout(30.0) == 10.0
        assert record_streams.list_request_timeout(0.2) == 1.0

    def test_a_good_reply_gives_its_people(self, monkeypatch):
        seen = []

        def fetch(address, timeout):
            seen.append((address, timeout))
            return (
                '[{"username": "Jevv", '
                '"stream": "http://bridge.test/1/camera.m3u8"}]'
            )

        monkeypatch.setattr(record_streams, "fetch_stream_list_text", fetch)

        entries = record_streams.read_list_entries("http://bridge.test/", 5.0)

        assert entries == [listed("Jevv")]
        assert seen == [("http://bridge.test/", 5.0)]

    def test_an_empty_array_is_a_readable_empty_list(self, monkeypatch):
        monkeypatch.setattr(
            record_streams, "fetch_stream_list_text", lambda address, timeout: "[]"
        )

        assert record_streams.read_list_entries("http://bridge.test/", 5.0) == []

    def test_a_non_array_reply_is_unreadable(self, monkeypatch):
        monkeypatch.setattr(
            record_streams,
            "fetch_stream_list_text",
            lambda address, timeout: '{"username": "Jevv"}',
        )

        assert record_streams.read_list_entries("http://bridge.test/", 5.0) is None

    def test_text_that_is_not_json_is_unreadable(self, monkeypatch):
        monkeypatch.setattr(
            record_streams,
            "fetch_stream_list_text",
            lambda address, timeout: "<html>Not Found</html>",
        )

        assert record_streams.read_list_entries("http://bridge.test/", 5.0) is None

    def test_a_failed_request_is_an_unreadable_reply(self, monkeypatch, caplog):
        # A refused connection or a timeout proves nothing about who is
        # streaming, so it must reach the cycle the same way a malformed reply
        # does: as None, so nothing is stopped and the warning is written once
        # rather than one error line per poll.
        caplog.set_level(logging.DEBUG)

        def fetch(address, timeout):
            raise OSError("connection refused")

        monkeypatch.setattr(record_streams, "fetch_stream_list_text", fetch)

        assert record_streams.read_list_entries("http://bridge.test/", 5.0) is None
        assert any(
            "connection refused" in record.getMessage() for record in caplog.records
        )


# ---------------------------------------------------------------------------
# The program entry point
# ---------------------------------------------------------------------------

class FakeClock:
    """A clock whose sleep ends the loop, so no test ever waits."""

    def __init__(self, work_seconds=2.0, sleeps_before_stop=1):
        self.now = 0.0
        self.work_seconds = work_seconds
        self.sleeps_before_stop = sleeps_before_stop
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if len(self.sleeps) >= self.sleeps_before_stop:
            raise KeyboardInterrupt


def run_program(
    monkeypatch,
    settings,
    replies,
    work_seconds=2.0,
    sleeps_before_stop=1,
    ffmpeg_found=True,
):
    """Patch every boundary ``main`` touches and return what happened.

    *replies* is the list the faked fetch hands out, one per iteration; an
    exception instance is raised instead, which is how a failed request looks.
    """
    clock = FakeClock(work_seconds, sleeps_before_stop)
    calls = {
        "fetch": [],
        "cycles": [],
        "stop_alls": 0,
        "log_levels": [],
        "engine_settings": None,
    }

    def fetch(address, timeout):
        calls["fetch"].append((address, timeout))
        if not replies:
            raise AssertionError("the test gave main more cycles than replies")
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def check_ffmpeg(engine_settings):
        calls["engine_settings"] = engine_settings
        return ffmpeg_found

    class FakeFarm:
        def __init__(self, farm_settings):
            calls["farm_settings"] = farm_settings

        def cycle(self, moment, entries):
            calls["cycles"].append(entries)
            clock.now += work_seconds

        def stop_all(self):
            calls["stop_alls"] += 1

    monkeypatch.setattr(record_streams, "load_dotenv", lambda: None)
    monkeypatch.setattr(record_streams, "load_streams_settings", lambda: settings)
    monkeypatch.setattr(
        record_streams,
        "configure_logging",
        lambda level: calls["log_levels"].append(level),
    )
    monkeypatch.setattr(record_streams, "ffmpeg_available", check_ffmpeg)
    monkeypatch.setattr(record_streams, "ListRecorderFarm", FakeFarm)
    monkeypatch.setattr(record_streams, "fetch_stream_list_text", fetch)
    monkeypatch.setattr(record_streams, "time", clock)
    return calls, clock


GOOD_REPLY = '[{"username": "Jevv", "stream": "http://bridge.test/1/camera.m3u8"}]'


class TestProgramEntry:
    """Startup checks, the poll loop, and the Ctrl+C way out."""

    def test_a_missing_recorder_program_exits_1_and_names_the_setting(
        self, monkeypatch, caplog
    ):
        caplog.set_level(logging.ERROR)
        calls, _clock = run_program(
            monkeypatch, make_settings(), [], ffmpeg_found=False
        )

        assert record_streams.main() == 1

        assert calls["fetch"] == []
        assert calls["stop_alls"] == 0
        assert any(
            record.levelno == logging.ERROR
            and "STREAMS_FFMPEG_PATH" in record.getMessage()
            for record in caplog.records
        )

    def test_the_startup_facts_are_logged_once(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        settings = make_settings(poll_seconds=5.0, clip_seconds=900, retry_limit=2)
        calls, _clock = run_program(monkeypatch, settings, [GOOD_REPLY])

        record_streams.main()

        text = "\n".join(messages(caplog))
        assert "http://127.0.0.1:8479/" in text
        assert r"D:\Clips" in text
        assert "Poll interval: 5 seconds" in text
        assert "Clip length: 900 seconds" in text
        assert "Retry Limit: 2" in text
        assert calls["log_levels"] == ["INFO"]
        assert calls["engine_settings"].output_dir == r"D:\Clips"

    def test_ctrl_c_stops_every_child_and_exits_0(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        calls, _clock = run_program(monkeypatch, make_settings(), [GOOD_REPLY])

        assert record_streams.main() == 0

        assert calls["stop_alls"] == 1
        assert calls["cycles"] == [[listed("Jevv")]]
        assert any(
            "stopped the List Recorder" in text for text in messages(caplog)
        )

    def test_one_request_is_fetched_with_the_poll_timeout(self, monkeypatch):
        settings = make_settings(poll_seconds=5.0)
        calls, _clock = run_program(monkeypatch, settings, [GOOD_REPLY])

        record_streams.main()

        assert calls["fetch"] == [("http://127.0.0.1:8479/", 5.0)]

    def test_the_loop_sleeps_the_rest_of_the_poll_interval(self, monkeypatch):
        settings = make_settings(poll_seconds=5.0)
        calls, clock = run_program(
            monkeypatch, settings, [GOOD_REPLY], work_seconds=2.0
        )

        record_streams.main()

        assert clock.sleeps == [3.0]

    def test_a_non_array_reply_reaches_the_cycle_as_unreadable(self, monkeypatch):
        calls, _clock = run_program(
            monkeypatch, make_settings(), ['{"username": "Jevv"}']
        )

        record_streams.main()

        assert calls["cycles"] == [None]

    def test_a_failed_request_reaches_the_cycle_as_unreadable(
        self, monkeypatch, caplog
    ):
        caplog.set_level(logging.ERROR)
        calls, _clock = run_program(
            monkeypatch,
            make_settings(),
            [OSError("connection refused"), GOOD_REPLY],
            sleeps_before_stop=2,
        )

        assert record_streams.main() == 0

        assert calls["cycles"] == [None, [listed("Jevv")]]
        assert calls["stop_alls"] == 1
        assert not [
            record for record in caplog.records if record.levelno == logging.ERROR
        ]

    def test_a_cycle_that_throws_never_ends_the_program(self, monkeypatch, caplog):
        caplog.set_level(logging.ERROR)

        class ExplodingFarm:
            def __init__(self, settings):
                pass

            def cycle(self, moment, entries):
                raise RuntimeError("boom")

            def stop_all(self):
                pass

        monkeypatch.setattr(record_streams, "load_dotenv", lambda: None)
        monkeypatch.setattr(
            record_streams, "load_streams_settings", lambda: make_settings()
        )
        monkeypatch.setattr(record_streams, "configure_logging", lambda level: None)
        monkeypatch.setattr(record_streams, "ffmpeg_available", lambda settings: True)
        monkeypatch.setattr(record_streams, "ListRecorderFarm", ExplodingFarm)
        monkeypatch.setattr(
            record_streams,
            "fetch_stream_list_text",
            lambda address, timeout: GOOD_REPLY,
        )
        monkeypatch.setattr(record_streams, "time", FakeClock())

        assert record_streams.main() == 0
        assert any(
            record.levelno == logging.ERROR
            and "while reading the Stream List" in record.getMessage()
            for record in caplog.records
        )
