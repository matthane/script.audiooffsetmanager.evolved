"""Unit tests for aom.store.offset_store.OffsetStore.

Plain pytest with tmp_path and a fixed fake clock; log sinks are collected
into lists so warnings/debug lines can be asserted. The doctrine pins live
here: delay_ms is a VERBATIM signed integer (no quantization, no range rule),
corruption is quarantined, a future schema is sacred (read-only, untouched),
and every persist is an atomic tmp-then-replace swap.
"""

import json
import os

import pytest

from resources.lib.aom.store.offset_store import OffsetStore

# 1752613391.0 epoch -> 2025-07-15T21:03:11Z (UTC).
FAKE_TS = 1752613391.0
EXPECTED_STAMP = "2025-07-15T21:03:11Z"

KEY = "dolbyvision|23|truehd"


def make_store(tmp_path, name="offsets.json", clock=None):
    debug = []
    warning = []
    path = str(tmp_path / name)
    store = OffsetStore(
        path,
        clock=clock or (lambda: FAKE_TS),
        log_debug=debug.append,
        log_warning=warning.append,
    )
    return store, path, debug, warning


# --- roundtrip ---------------------------------------------------------------

def test_roundtrip_across_instances(tmp_path):
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    assert store.set(KEY, 175, source="user", video_fps=23.976) is True

    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert reopened.get(KEY) == store.get(KEY)
    assert reopened.get(KEY)["delay_ms"] == 175
    assert reopened.get(KEY)["video_fps"] == 23.976


# --- verbatim pins (doctrine) ------------------------------------------------

@pytest.mark.parametrize("value", [-115, -2500, 9999, 7, -3, 113, 12345, 0])
def test_delay_values_store_verbatim(tmp_path, value):
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, value)

    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    stored = reopened.get(KEY)["delay_ms"]
    assert stored == value
    assert type(stored) is int


# The parametrized pin above IS the whole contract: 113 pins "no 25/5 ms
# snapping" and 12345 pins "no range rule (even beyond ±10 s)" — do not
# narrow its value list without replacing the coverage.


# --- ValueError guards -------------------------------------------------------

@pytest.mark.parametrize("bad", ["100", 1.5, True, False, None])
def test_non_int_delay_raises(tmp_path, bad):
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    with pytest.raises(ValueError):
        store.set(KEY, bad)


@pytest.mark.parametrize("bad_key", ["", 5, None, b"bytes"])
def test_bad_key_raises(tmp_path, bad_key):
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    with pytest.raises(ValueError):
        store.set(bad_key, 100)


# --- timestamps --------------------------------------------------------------

def test_timestamp_exact(tmp_path):
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    assert store.get(KEY)["updated"] == EXPECTED_STAMP


def test_second_set_refreshes_timestamp(tmp_path):
    clock = {"t": FAKE_TS}
    store, _path, _debug, _warning = make_store(tmp_path, clock=lambda: clock["t"])
    store.load()
    store.set(KEY, 100)
    first = store.get(KEY)["updated"]

    clock["t"] = FAKE_TS + 3600  # one hour later
    store.set(KEY, 200)
    second = store.get(KEY)["updated"]
    assert second == "2025-07-15T22:03:11Z"
    assert second != first


# --- video_fps optionality ---------------------------------------------------

def test_video_fps_present_when_passed(tmp_path):
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100, video_fps=59.94)
    assert store.get(KEY)["video_fps"] == 59.94


def test_video_fps_absent_when_omitted(tmp_path):
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    assert "video_fps" not in store.get(KEY)


# --- delete ------------------------------------------------------------------

def test_delete_existing_persists(tmp_path):
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    assert store.delete(KEY) is True
    assert store.get(KEY) is None

    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert reopened.get(KEY) is None


def test_delete_missing_does_not_write(tmp_path):
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set("other|23|ac3", 50)
    before = open(path, "rb").read()

    assert store.delete(KEY) is False
    after = open(path, "rb").read()
    assert after == before
    assert not os.path.exists(path + ".tmp")


# --- clear -------------------------------------------------------------------

def test_clear_returns_count_and_persists_empty(tmp_path):
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    store.set("other|24|ac3", 50)
    assert store.clear() == 2
    assert len(store) == 0

    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert len(reopened) == 0
    on_disk = json.loads(open(path, "r", encoding="utf-8").read())
    assert on_disk == {"version": 1, "profiles": {}}


def test_clear_on_empty_writes_nothing(tmp_path):
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    assert store.clear() == 0
    assert not os.path.exists(path)


# --- corruption --------------------------------------------------------------

def test_corruption_quarantines_and_recovers(tmp_path):
    store, path, _debug, warning = make_store(tmp_path)
    junk = b"\x00\x01 not json at all {{{"
    with open(path, "wb") as handle:
        handle.write(junk)

    store.load()
    assert len(store) == 0
    assert os.path.exists(path + ".bad")
    assert open(path + ".bad", "rb").read() == junk
    assert len(warning) == 1
    assert store.pop_corruption() is True
    assert store.pop_corruption() is False

    # A subsequent set recreates a valid file.
    assert store.set(KEY, 175) is True
    on_disk = json.loads(open(path, "r", encoding="utf-8").read())
    assert on_disk["version"] == 1
    assert on_disk["profiles"][KEY]["delay_ms"] == 175


def test_bad_overwrites_older_bad(tmp_path):
    store, path, _debug, _warning = make_store(tmp_path)
    with open(path + ".bad", "wb") as handle:
        handle.write(b"older bad")
    with open(path, "wb") as handle:
        handle.write(b"newer junk {{{")

    store.load()
    assert open(path + ".bad", "rb").read() == b"newer junk {{{"


@pytest.mark.parametrize("blob", [
    "[]",                              # top-level not a dict
    '{"version": 1}',                  # profiles missing
    '{"version": "x", "profiles": {}}',  # version not an int
    '{"version": true, "profiles": {}}',  # bool version is not a valid int
    '{"profiles": {}}',                # version missing
    '{"version": 1, "profiles": []}',  # profiles not a dict
])
def test_wrong_shapes_are_corruption(tmp_path, blob):
    store, path, _debug, warning = make_store(tmp_path)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(blob)

    store.load()
    assert store.pop_corruption() is True
    assert os.path.exists(path + ".bad")
    assert len(store) == 0
    assert len(warning) == 1


# --- missing file ------------------------------------------------------------

def test_missing_file_is_clean(tmp_path):
    store, path, _debug, warning = make_store(tmp_path)
    store.load()
    assert len(store) == 0
    assert not os.path.exists(path + ".bad")
    assert store.pop_corruption() is False
    assert warning == []


# --- future schema version ---------------------------------------------------

def test_future_version_is_read_only_and_untouched(tmp_path):
    store, path, _debug, warning = make_store(tmp_path)
    original = '{"version": 2, "profiles": {"k": {"delay_ms": 5}}}'
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(original)

    store.load()
    assert len(store) == 0
    assert store.pop_corruption() is False
    assert not os.path.exists(path + ".bad")
    # File bytes unchanged after load.
    assert open(path, "r", encoding="utf-8").read() == original
    assert len(warning) == 1

    # A mutator refuses and leaves the file untouched.
    assert store.set(KEY, 100) is False
    assert store.delete("k") is False
    assert store.clear() == 0
    assert open(path, "r", encoding="utf-8").read() == original


# --- lenient per-entry drop --------------------------------------------------

def test_lenient_entry_drop(tmp_path):
    store, path, debug, warning = make_store(tmp_path)
    payload = {
        "version": 1,
        "profiles": {
            "good|23|truehd": {"delay_ms": 175, "updated": EXPECTED_STAMP,
                               "source": "user"},
            "bad|24|ac3": {"delay_ms": "abc"},       # non-int delay -> dropped
            "notdict|25|eac3": "oops",               # not a dict -> dropped
            "boolish|29|dca": {"delay_ms": True},    # bool delay -> dropped
        },
    }
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))

    store.load()
    assert store.get("good|23|truehd")["delay_ms"] == 175
    assert store.get("bad|24|ac3") is None
    assert store.get("notdict|25|eac3") is None
    assert store.get("boolish|29|dca") is None
    assert len(store) == 1
    # Per-entry damage is not whole-file corruption.
    assert store.pop_corruption() is False
    assert not os.path.exists(path + ".bad")
    assert len(debug) == 3  # one debug line per dropped entry


# --- atomic swap failure -----------------------------------------------------

def test_atomic_swap_failure_leaves_original_intact(tmp_path, monkeypatch):
    store, path, _debug, warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)  # establishes a valid original on disk
    original = open(path, "rb").read()

    import resources.lib.aom.store.offset_store as module
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(module.os, "replace", flaky_replace)
    assert store.set(KEY, 999) is False
    monkeypatch.undo()

    # Original file content survived the failed swap.
    assert open(path, "rb").read() == original
    assert len(warning) == 1
    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert reopened.get(KEY)["delay_ms"] == 100


def test_clear_reports_persist_failure_as_zero(tmp_path, monkeypatch):
    # Same contract as delete: a clear whose persist fails must not ack
    # "cleared N" — the entries resurrect from disk on the next load.
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    store.set(KEY + "2", 200)

    import resources.lib.aom.store.offset_store as module

    def broken_replace(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(module.os, "replace", broken_replace)
    assert store.clear() == 0
    monkeypatch.undo()

    assert len(store) == 0  # in-memory removal stands
    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert len(reopened) == 2  # but the file still holds both


def test_version_zero_is_quarantined_not_loaded(tmp_path):
    # The schema started at 1: version 0 (or negative) never existed and
    # must quarantine like corruption, never load-and-resave as v1 data.
    store, path, _debug, warning = make_store(tmp_path)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"version": 0, "profiles": {"k|all|a": {"delay_ms": 5}}}')
    store.load()
    assert len(store) == 0
    assert store.pop_corruption() is True
    assert os.path.exists(path + ".bad")


def test_nonfinite_video_fps_is_rejected(tmp_path):
    # NaN/Infinity would serialize as bare tokens that are not valid JSON.
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    for bad in (float("nan"), float("inf"), float("-inf"), True, "23.976"):
        with pytest.raises(ValueError):
            store.set(KEY, 100, video_fps=bad)
    # Finite numbers and None are fine.
    assert store.set(KEY, 100, video_fps=23.976) is True
    assert store.set(KEY, 100, video_fps=24) is True
    assert store.set(KEY, 100) is True


def test_read_only_property_reflects_future_version(tmp_path):
    store, path, _debug, _warning = make_store(tmp_path)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"version": 2, "profiles": {}}')
    store.load()
    assert store.read_only is True

    fresh, _p, _d, _w = make_store(tmp_path / "elsewhere")
    fresh.load()
    assert fresh.read_only is False


def test_delete_reports_persist_failure(tmp_path, monkeypatch):
    # A delete whose persist fails must return False: the entry would
    # resurrect from disk on the next load, and the mutation-channel ack
    # (E4) must not claim durability the file does not have.
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)

    import resources.lib.aom.store.offset_store as module

    def broken_replace(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(module.os, "replace", broken_replace)
    assert store.delete(KEY) is False
    monkeypatch.undo()

    # In-memory removal stands (consistent with set's failure semantics),
    # but the on-disk file still holds the entry.
    assert store.get(KEY) is None
    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert reopened.get(KEY)["delay_ms"] == 100


# --- get returns a copy ------------------------------------------------------

def test_get_returns_copy(tmp_path):
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    grabbed = store.get(KEY)
    grabbed["delay_ms"] = 9999
    grabbed["source"] = "tampered"
    assert store.get(KEY)["delay_ms"] == 100
    assert store.get(KEY)["source"] == "user"


def test_entries_returns_copies(tmp_path):
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    snapshot = store.entries()
    snapshot[KEY]["delay_ms"] = -1
    assert store.get(KEY)["delay_ms"] == 100


# --- parent-dir creation -----------------------------------------------------

def test_parent_dir_created(tmp_path):
    nested = tmp_path / "sub" / "dir" / "offsets.json"
    debug = []
    warning = []
    store = OffsetStore(str(nested), clock=lambda: FAKE_TS,
                        log_debug=debug.append, log_warning=warning.append)
    store.load()
    assert store.set(KEY, 175) is True
    assert nested.exists()
    assert warning == []


# --- read_profiles: the other-process (management view) reader ----------------

def _read_profiles(path):
    from resources.lib.aom.store.offset_store import read_profiles
    return read_profiles(path)


def test_read_profiles_missing_file_is_empty(tmp_path):
    assert _read_profiles(str(tmp_path / "offsets.json")) == {}


def test_read_profiles_roundtrips_written_entries(tmp_path):
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, -115, video_fps=23.976)

    entries = _read_profiles(path)
    assert entries[KEY]["delay_ms"] == -115
    assert entries[KEY]["video_fps"] == 23.976


def test_read_profiles_never_quarantines_a_corrupt_file(tmp_path):
    # The script process must not mutate the file (single-writer doctrine):
    # unlike load(), a corrupt file raises instead of renaming to .bad.
    from resources.lib.aom.store.offset_store import StoreUnreadable
    path = tmp_path / "offsets.json"
    path.write_text("junk {{{", encoding="utf-8")

    with pytest.raises(StoreUnreadable):
        _read_profiles(str(path))
    assert path.exists()                              # untouched
    assert not (tmp_path / "offsets.json.bad").exists()


def test_read_profiles_refuses_future_schema_untouched(tmp_path):
    from resources.lib.aom.store.offset_store import StoreUnreadable
    path = tmp_path / "offsets.json"
    blob = json.dumps({"version": 2, "profiles": {KEY: {"delay_ms": 5}}})
    path.write_text(blob, encoding="utf-8")

    with pytest.raises(StoreUnreadable):
        _read_profiles(str(path))
    assert path.read_text(encoding="utf-8") == blob   # byte-identical


def test_read_profiles_filters_malformed_entries_like_load(tmp_path):
    path = tmp_path / "offsets.json"
    path.write_text(json.dumps({"version": 1, "profiles": {
        KEY: {"delay_ms": -115},
        "bad|entry|shape": "not a dict",
        "bad|delay|type": {"delay_ms": "fast"},
        "bool|delay|guard": {"delay_ms": True},
    }}), encoding="utf-8")

    entries = _read_profiles(str(path))
    assert set(entries) == {KEY}
