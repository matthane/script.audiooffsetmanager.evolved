"""Unit tests for aome.store.offset_store.OffsetStore.

Plain pytest with tmp_path and a fixed fake clock; log sinks are collected
into lists so warnings/debug lines can be asserted. The doctrine pins live
here: delay_ms is a VERBATIM signed integer (no quantization, no range rule),
corruption is quarantined, a future schema is sacred (read-only, untouched),
and every persist is an atomic tmp-then-replace swap.
"""

import json
import os

import pytest

from resources.lib.aome.store.offset_store import OffsetStore

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
    # Clear-all leaves a reset marker per removed key: the "expect 0
    # next time" contract holds for clear too.
    on_disk = json.loads(open(path, "r", encoding="utf-8").read())
    assert on_disk == {"version": 1, "profiles": {},
                       "resets": sorted([KEY, "other|24|ac3"])}
    assert reopened.reset_pending(KEY)
    assert reopened.reset_pending("other|24|ac3")


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

    import resources.lib.aome.store.offset_store as module

    def broken_replace(src, dst):
        # PERSISTENT failure: every attempt (including the sharing-
        # violation retries) fails, so the persist genuinely misses.
        raise OSError("simulated replace failure")

    monkeypatch.setattr(module.os, "replace", broken_replace)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    assert store.set(KEY, 999) is False
    monkeypatch.undo()

    # Original file content survived the failed swap.
    assert open(path, "rb").read() == original
    assert len(warning) == 1
    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert reopened.get(KEY)["delay_ms"] == 100


def test_transient_replace_failure_is_retried_and_recovers(tmp_path,
                                                           monkeypatch):
    # On Windows a concurrent reader (the management view's
    # read_profiles in the script process) holding offsets.json open makes
    # os.replace fail with a sharing violation for a sub-millisecond
    # window. One transient failure must not lose the write.
    store, path, _debug, warning = make_store(tmp_path)
    store.load()

    import resources.lib.aome.store.offset_store as module
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr(module.os, "replace", flaky_replace)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)

    assert store.set(KEY, -115) is True
    assert warning == []
    monkeypatch.undo()

    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert reopened.get(KEY)["delay_ms"] == -115


def test_read_profiles_names_dropped_entries_through_log_debug(tmp_path):
    # load() names every dropped entry; the reader must too, or
    # a hand-edited entry vanishes from the management view untraceably.
    path = tmp_path / "offsets.json"
    path.write_text(json.dumps({"version": 1, "profiles": {
        KEY: {"delay_ms": -115},
        "bad|delay|type": {"delay_ms": "fast"},
    }}), encoding="utf-8")

    from resources.lib.aome.store.offset_store import read_profiles
    lines = []
    entries = read_profiles(str(path), log_debug=lines.append)

    assert set(entries) == {KEY}
    assert any("bad|delay|type" in line for line in lines)


def test_clear_reports_persist_failure_as_zero(tmp_path, monkeypatch):
    # Same contract as delete: a clear whose persist fails must not ack
    # "cleared N" — the entries resurrect from disk on the next load.
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    store.set(KEY + "2", 200)

    import resources.lib.aome.store.offset_store as module

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
    # must not claim durability the file does not have.
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)

    import resources.lib.aome.store.offset_store as module

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
    from resources.lib.aome.store.offset_store import read_profiles
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
    from resources.lib.aome.store.offset_store import StoreUnreadable
    path = tmp_path / "offsets.json"
    path.write_text("junk {{{", encoding="utf-8")

    with pytest.raises(StoreUnreadable) as excinfo:
        _read_profiles(str(path))
    assert excinfo.value.future is False              # corrupt, not newer
    assert path.exists()                              # untouched
    assert not (tmp_path / "offsets.json.bad").exists()


def test_read_profiles_refuses_future_schema_untouched(tmp_path):
    from resources.lib.aome.store.offset_store import StoreUnreadable
    path = tmp_path / "offsets.json"
    blob = json.dumps({"version": 2, "profiles": {KEY: {"delay_ms": 5}}})
    path.write_text(blob, encoding="utf-8")

    with pytest.raises(StoreUnreadable) as excinfo:
        _read_profiles(str(path))
    # future=True: the view words this as "preserved, not shown" — NEVER
    # as the corrupt case's quarantine-and-reset promise.
    assert excinfo.value.future is True
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


# --- reset markers ----------------------------------

def test_delete_leaves_a_persisted_reset_marker(tmp_path):
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    assert store.delete(KEY) is True
    assert store.reset_pending(KEY)

    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert reopened.reset_pending(KEY)
    on_disk = json.loads(open(path, "r", encoding="utf-8").read())
    assert on_disk["resets"] == [KEY]


def test_consume_reset_removes_the_marker_durably(tmp_path):
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    store.delete(KEY)
    assert store.consume_reset(KEY) is True
    assert not store.reset_pending(KEY)

    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert not reopened.reset_pending(KEY)
    # An empty marker set writes NO resets section: the file shape is
    # byte-compatible with the pre-marker format.
    on_disk = json.loads(open(path, "r", encoding="utf-8").read())
    assert "resets" not in on_disk


def test_consume_of_absent_marker_touches_no_disk(tmp_path):
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    before = open(path, "rb").read()
    assert store.consume_reset("never|all|deleted") is True
    assert open(path, "rb").read() == before


def test_set_supersedes_a_pending_reset(tmp_path):
    # Re-learning the profile before it plays again cancels the promised
    # 0: the fresh value is the user's newest intent.
    store, _path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    store.delete(KEY)
    assert store.reset_pending(KEY)
    store.set(KEY, -75)
    assert not store.reset_pending(KEY)

    reopened, _p, _d, _w = make_store(tmp_path)
    reopened.load()
    assert not reopened.reset_pending(KEY)
    assert reopened.get(KEY)["delay_ms"] == -75


def test_scribbled_resets_section_degrades_to_no_markers(tmp_path):
    # Hand-edited files: a foreign resets shape drops with a debug line,
    # never crashes and never invents a spurious 0.
    path = str(tmp_path / "offsets.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1,
                   "profiles": {KEY: {"delay_ms": 5}},
                   "resets": {"not": "a list"}}, handle)
    store, _path, debug, _warning = make_store(tmp_path)
    store.load()
    assert store.get(KEY)["delay_ms"] == 5
    assert not store.reset_pending(KEY)
    assert any("non-list resets" in line for line in debug)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "profiles": {},
                   "resets": [KEY, 7, "", None]}, handle)
    store2, _p2, debug2, _w2 = make_store(tmp_path)
    store2.load()
    assert store2.reset_pending(KEY)
    assert sum("non-string reset marker" in line for line in debug2) == 3


def test_read_profiles_never_shows_reset_markers(tmp_path):
    # The management view lists offsets; a pending reset is not an offset.
    from resources.lib.aome.store.offset_store import read_profiles
    store, path, _debug, _warning = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    store.set("other|24|ac3", 50)
    store.delete(KEY)
    entries = read_profiles(path)
    assert set(entries) == {"other|24|ac3"}


# --- read_import: the backup restore-source reader -----------------------------

def _read_import(path):
    from resources.lib.aome.store.offset_store import read_import
    return read_import(path)


def test_read_import_missing_file_raises(tmp_path):
    # THE divergence from read_profiles: an absent restore source is a
    # failed import, never "replace everything with nothing".
    from resources.lib.aome.store.offset_store import StoreUnreadable
    with pytest.raises(StoreUnreadable):
        _read_import(str(tmp_path / "offsets.json.import"))


def test_read_import_roundtrips_a_written_store_file(tmp_path):
    store, path, _d, _w = make_store(tmp_path)
    store.load()
    store.set(KEY, -115, video_fps=23.976)
    entries = _read_import(path)
    assert entries[KEY]["delay_ms"] == -115
    assert entries[KEY]["video_fps"] == 23.976


def test_read_import_rejects_corrupt_and_future_files(tmp_path):
    from resources.lib.aome.store.offset_store import StoreUnreadable
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{nope", encoding="utf-8")
    with pytest.raises(StoreUnreadable) as excinfo:
        _read_import(str(corrupt))
    assert excinfo.value.future is False

    future = tmp_path / "future.json"
    future.write_text(json.dumps({"version": 99, "profiles": {}}),
                      encoding="utf-8")
    with pytest.raises(StoreUnreadable) as excinfo:
        _read_import(str(future))
    assert excinfo.value.future is True


def test_read_import_filters_malformed_entries_like_load(tmp_path):
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({
        "version": 1,
        "profiles": {
            KEY: {"delay_ms": 175},
            "scribble": "not-a-dict",
            "boolish": {"delay_ms": True},
        },
    }), encoding="utf-8")
    assert set(_read_import(str(path))) == {KEY}


# --- replace_all: the import/restore write ------------------------------------

def test_replace_all_replaces_everything_and_persists(tmp_path):
    store, path, _d, _w = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    store.set("hdr10|all|ac3", 50)

    imported = {"hlg|all|eac3": {"delay_ms": -75, "updated": "x",
                                 "source": "user"}}
    assert store.replace_all(imported) is True
    assert store.get(KEY) is None
    assert store.get("hlg|all|eac3")["delay_ms"] == -75
    assert len(store) == 1

    reopened, _p, _d2, _w2 = make_store(tmp_path)
    reopened.load()
    assert set(reopened.entries()) == {"hlg|all|eac3"}


def test_replace_all_marks_dropped_keys_for_reset(tmp_path):
    # Restore semantics inherit delete/clear's contract: a key the backup
    # does not carry means "expect 0 the next time it plays".
    store, _path, _d, _w = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    store.set("hdr10|all|ac3", 50)

    store.replace_all({"hdr10|all|ac3": {"delay_ms": 60}})
    assert store.reset_pending(KEY) is True
    assert store.reset_pending("hdr10|all|ac3") is False

    reopened, _p, _d2, _w2 = make_store(tmp_path)
    reopened.load()
    assert reopened.reset_pending(KEY) is True


def test_replace_all_supersedes_pending_markers_it_covers(tmp_path):
    # A pending reset whose key the import (re)fills is superseded, like
    # set(); one for a key the import still lacks stays pending.
    store, _path, _d, _w = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    store.set("hdr10|all|ac3", 50)
    store.delete(KEY)                       # marker for KEY
    store.delete("hdr10|all|ac3")           # marker for hdr10

    store.replace_all({KEY: {"delay_ms": 25}})
    assert store.reset_pending(KEY) is False
    assert store.reset_pending("hdr10|all|ac3") is True


def test_replace_all_filters_malformed_entries(tmp_path):
    # The write path re-filters (defense in depth: it must stay safe even
    # when the caller is not read_import).
    store, _path, _d, _w = make_store(tmp_path)
    store.load()
    store.replace_all({
        KEY: {"delay_ms": -115},
        "scribble": "not-a-dict",
        "boolish": {"delay_ms": True},
    })
    assert set(store.entries()) == {KEY}


def test_replace_all_with_empty_dict_acts_like_clear(tmp_path):
    store, _path, _d, _w = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    assert store.replace_all({}) is True
    assert len(store) == 0
    assert store.reset_pending(KEY) is True


def test_replace_all_refused_when_read_only(tmp_path):
    path = tmp_path / "offsets.json"
    original = {"version": 99, "profiles": {KEY: {"delay_ms": 1}}}
    path.write_text(json.dumps(original), encoding="utf-8")
    store, _p, _d, warning = make_store(tmp_path)
    store.load()

    assert store.replace_all({KEY: {"delay_ms": 5}}) is False
    assert any("read-only" in line for line in warning)
    # The future file is untouched (the future is sacred).
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_replace_all_reports_persist_failure_with_memory_standing(
        tmp_path, monkeypatch):
    store, _path, _d, _w = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)
    monkeypatch.setattr(store, "_persist", lambda: False)

    assert store.replace_all({"hlg|all|eac3": {"delay_ms": -75}}) is False
    # In-memory replacement stands, consistent with set/delete/clear.
    assert store.get("hlg|all|eac3")["delay_ms"] == -75
    assert store.get(KEY) is None
    assert store.reset_pending(KEY) is True


# --- read_import_document / discard_import (the restore round trip) ------------

def test_read_import_document_carries_validated_reset_markers(tmp_path):
    from resources.lib.aome.store.offset_store import read_import_document
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({
        "version": 1,
        "profiles": {KEY: {"delay_ms": 175}},
        "resets": ["sdr|all|aac", "", 7, None],
    }), encoding="utf-8")

    entries, resets = read_import_document(str(path))
    assert set(entries) == {KEY}
    # Same rules as load(): non-string/empty markers dropped, keys kept.
    assert resets == {"sdr|all|aac"}


def test_read_import_document_without_resets_section_is_empty(tmp_path):
    from resources.lib.aome.store.offset_store import read_import_document
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({"version": 1,
                                "profiles": {KEY: {"delay_ms": 1}}}),
                    encoding="utf-8")
    assert read_import_document(str(path))[1] == set()


def test_replace_all_carries_backup_reset_markers(tmp_path):
    # The restore preserves the backup's own pending "expect 0" promises,
    # minus any key the import (re)fills.
    store, _path, _d, _w = make_store(tmp_path)
    store.load()
    store.set(KEY, 100)

    store.replace_all({"hdr10|all|ac3": {"delay_ms": 60}},
                      resets=["sdr|all|aac", "hdr10|all|ac3"])
    assert store.reset_pending("sdr|all|aac") is True     # carried
    assert store.reset_pending("hdr10|all|ac3") is False  # superseded
    assert store.reset_pending(KEY) is True               # dropped live key

    reopened, _p, _d2, _w2 = make_store(tmp_path)
    reopened.load()
    assert reopened.reset_pending("sdr|all|aac") is True


def test_discard_import_removes_file_and_tolerates_absence(tmp_path):
    from resources.lib.aome.store.offset_store import discard_import
    staged = tmp_path / "offsets.json.import"
    staged.write_text("{}", encoding="utf-8")

    discard_import(str(staged))
    assert not staged.exists()
    # Already consumed: the missing-file case is normal and silent.
    warnings = []
    discard_import(str(staged), log_warning=warnings.append)
    assert warnings == []
