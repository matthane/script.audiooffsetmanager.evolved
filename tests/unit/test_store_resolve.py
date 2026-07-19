"""The key-schema decision table, row by row.

Every behavior here is a decided rule, not an implementation detail:
one candidate key per toggle state (no fallback between the levels),
dormancy in both directions, miss = None, and the write rule
(store-instant derivation, never lookup-dependent).
"""

import pytest

from resources.lib.aome.store import resolve
from resources.lib.aome.store.offset_store import OffsetStore


def make_store(tmp_path):
    store = OffsetStore(str(tmp_path / "offsets.json"))
    store.load()
    return store


# --- toggle OFF: the fps axis does not exist --------------------------------

def test_off_hits_the_all_key_as_exact(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd", 175)
    got = resolve.resolve(store, "DolbyVision", 23.976, "TrueHD", per_fps=False)
    assert got.entry["delay_ms"] == 175
    assert got.hit_kind == resolve.EXACT
    assert got.key == "dolbyvision|all|truehd"


def test_off_empty_store_is_a_miss(tmp_path):
    store = make_store(tmp_path)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", per_fps=False)
    assert got.entry is None
    assert got.hit_kind == resolve.MISS
    assert got.key is None                       # stable meaning: no hit
    assert got.tried == ("dolbyvision|all|truehd",)


def test_off_specific_entries_are_dormant(tmp_path):
    # A specific-fps entry taught while the toggle was ON is never matched
    # while it is OFF — dormant, not deleted (decision table row).
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd", 175)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", per_fps=False)
    assert got.hit_kind == resolve.MISS


def test_off_ignores_the_fps_value_entirely(tmp_path):
    # With the toggle off the fps argument must not even be parsed —
    # None/garbage flows through because the axis does not exist.
    store = make_store(tmp_path)
    store.set("sdr|all|aac", -50)
    got = resolve.resolve(store, "sdr", None, "aac", per_fps=False)
    assert got.entry["delay_ms"] == -50


# --- toggle ON: the specific key or nothing ----------------------------------

def test_on_exact_hit(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd", 175)
    store.set("dolbyvision|all|truehd", 100)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", per_fps=True)
    assert got.entry["delay_ms"] == 175
    assert got.hit_kind == resolve.EXACT
    assert got.key == "dolbyvision|23|truehd"


def test_on_all_entries_are_dormant(tmp_path):
    # STRICT: an offset saved while the toggle was off covers all frame
    # rates and is NOT applied while the toggle is on — no fallback level
    # exists. Dormant, not deleted (the mirror of the OFF-side dormancy
    # row above).
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd", 100)
    got = resolve.resolve(store, "dolbyvision", 60.0, "truehd", per_fps=True)
    assert got.entry is None
    assert got.hit_kind == resolve.MISS
    assert got.key is None
    assert got.tried == ("dolbyvision|60|truehd",)  # the all key: not consulted


def test_on_miss_for_an_untaught_rate(tmp_path):
    # A specific entry for a DIFFERENT fps does not serve sideways:
    # the candidate is a single key, so 23's entry cannot serve 60.
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd", 175)
    got = resolve.resolve(store, "dolbyvision", 60.0, "truehd", per_fps=True)
    assert got.entry is None
    assert got.hit_kind == resolve.MISS
    assert got.tried == ("dolbyvision|60|truehd",)


def test_on_fractional_rates_resolve_their_own_keys(tmp_path):
    # 23.976 and 24.0 are different HDMI modes with different latencies:
    # distinct keys, independent values (design guarantee).
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd", 175)
    store.set("dolbyvision|24|truehd", 120)
    got23 = resolve.resolve(store, "dolbyvision", 23.976, "truehd", per_fps=True)
    got24 = resolve.resolve(store, "dolbyvision", 24.0, "truehd", per_fps=True)
    assert got23.entry["delay_ms"] == 175
    assert got24.entry["delay_ms"] == 120


# --- toggle flips are non-destructive, dormancy is symmetric ------------------

def test_flip_off_then_on_leaves_all_dormant_but_stored(tmp_path):
    store = make_store(tmp_path)
    key = resolve.write_key("dolbyvision", None, "truehd", per_fps=False)
    store.set(key, 100)
    assert resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                           per_fps=True).hit_kind == resolve.MISS
    # Still in the store (management view sees it; OFF reaches it again).
    assert store.get(key)["delay_ms"] == 100
    assert resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                           per_fps=False).entry["delay_ms"] == 100


def test_flip_on_then_off_leaves_specific_dormant_but_stored(tmp_path):
    store = make_store(tmp_path)
    key = resolve.write_key("dolbyvision", 23.976, "truehd", per_fps=True)
    store.set(key, 175)
    assert resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                           per_fps=False).hit_kind == resolve.MISS
    # Still in the store (management view sees it; ON reaches it again).
    assert store.get(key)["delay_ms"] == 175
    assert resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                           per_fps=True).entry["delay_ms"] == 175


# --- the write rule ---------------------------------------------------------

def test_write_key_off_targets_the_all_key(tmp_path):
    assert resolve.write_key("DolbyVision", 23.976, "TrueHD",
                             per_fps=False) == "dolbyvision|all|truehd"


def test_write_key_on_targets_the_specific_key(tmp_path):
    assert resolve.write_key("DolbyVision", 23.976, "TrueHD",
                             per_fps=True) == "dolbyvision|23|truehd"


def test_write_then_resolve_roundtrips_as_exact(tmp_path):
    # The re-teach flow: an all-level offset taught with the toggle off is
    # dormant once it is on; teaching 60 writes the specific key and 60
    # hits exact, while 24 stays a miss until taught itself.
    store = make_store(tmp_path)
    store.set(resolve.write_key("dv", None, "truehd", per_fps=False), 175)

    at60 = resolve.resolve(store, "dv", 60.0, "truehd", per_fps=True)
    assert (at60.hit_kind, at60.entry) == (resolve.MISS, None)

    store.set(resolve.write_key("dv", 60.0, "truehd", per_fps=True), 120)
    at60 = resolve.resolve(store, "dv", 60.0, "truehd", per_fps=True)
    assert (at60.hit_kind, at60.entry["delay_ms"]) == (resolve.EXACT, 120)
    at24 = resolve.resolve(store, "dv", 24.0, "truehd", per_fps=True)
    assert (at24.hit_kind, at24.entry) == (resolve.MISS, None)


# --- open vocabulary flows straight through ------------------------------------

def test_unheard_of_formats_resolve_like_any_other(tmp_path):
    store = make_store(tmp_path)
    key = resolve.write_key("HDR10+", 119.88, "x-future-codec", per_fps=True)
    assert key == "hdr10plus|119|x-future-codec"
    store.set(key, -115)
    got = resolve.resolve(store, "HDR10+", 119.88, "x-future-codec", per_fps=True)
    assert (got.hit_kind, got.entry["delay_ms"]) == (resolve.EXACT, -115)


def test_legacy_spelled_file_entries_resolve_after_load(tmp_path):
    # End-to-end boundary guarantee: an offsets.json written by an older
    # codec ('hdr10+', spaced 'dolby vision') still resolves for the
    # profiles live detection produces today, because load() re-keys at
    # the store boundary.
    import json
    path = tmp_path / "offsets.json"
    path.write_text(json.dumps({"version": 1, "profiles": {
        "hdr10+|all|truehd": {"delay_ms": -40},
        "dolby vision|all|truehd_atmos": {"delay_ms": 50},
    }}), encoding="utf-8")
    store = OffsetStore(str(path))
    store.load()
    got = resolve.resolve(store, "HDR10+", 23.976, "TrueHD", per_fps=False)
    assert (got.hit_kind, got.entry["delay_ms"]) == (resolve.EXACT, -40)
    got = resolve.resolve(store, "Dolby Vision", 23.976, "truehd_atmos",
                          per_fps=False)
    assert (got.hit_kind, got.entry["delay_ms"]) == (resolve.EXACT, 50)


def test_on_with_unparseable_fps_consults_the_all_key(tmp_path):
    # resolve() is total: an fps that cannot be parsed means the stream
    # has NO fps axis, so the all key IS its exact key — the same meaning
    # 'all' carries with the toggle off (a benign miss must never become
    # an exception in the apply path). Defensive only: completeness
    # gating keeps unparseable rates out of the production apply path.
    store = make_store(tmp_path)
    store.set("sdr|all|aac", -50)
    got = resolve.resolve(store, "sdr", "abc", "aac", per_fps=True)
    assert got.entry["delay_ms"] == -50
    assert got.hit_kind == resolve.EXACT
    assert got.tried == ("sdr|all|aac",)  # the specific level never composed


def test_on_with_unparseable_fps_and_empty_store_is_a_miss(tmp_path):
    store = make_store(tmp_path)
    got = resolve.resolve(store, "sdr", None, "aac", per_fps=True)
    assert (got.entry, got.hit_kind, got.key) == (None, resolve.MISS, None)
    assert got.tried == ("sdr|all|aac",)


def test_write_key_stays_strict_on_unparseable_fps(tmp_path):
    # The WRITE side keeps the loud contract: storing under a garbage key
    # is worse than failing — writers are gated on verified profiles.
    with pytest.raises(ValueError):
        resolve.write_key("sdr", "abc", "aac", per_fps=True)


def test_resolution_ms_accessor_keeps_entry_shape_internal(tmp_path):
    # Consumers read .ms instead of indexing entry['delay_ms'] (the entry
    # dict shape stays inside the store package).
    store = make_store(tmp_path)
    store.set("sdr|all|aac", -115)
    hit = resolve.resolve(store, "sdr", None, "aac", per_fps=False)
    assert hit.ms == -115
    miss = resolve.resolve(store, "sdr", None, "flac", per_fps=False)
    assert miss.ms is None


# --- reset markers on the consulted key -----------

def test_marked_miss_carries_the_reset_key(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd", 175)
    store.delete("dolbyvision|all|truehd")
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                          per_fps=False)
    assert got.hit_kind == resolve.MISS
    assert got.reset_keys == ("dolbyvision|all|truehd",)


def test_on_marked_miss_carries_the_specific_key_only(tmp_path):
    # Only the consulted key's marker travels: the deleted all-level
    # marker is dormant while the toggle is on, exactly like the entries.
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd", 175)
    store.set("dolbyvision|all|truehd", -25)
    store.delete("dolbyvision|23|truehd")
    store.delete("dolbyvision|all|truehd")
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                          per_fps=True)
    assert got.hit_kind == resolve.MISS
    assert got.reset_keys == ("dolbyvision|23|truehd",)


def test_on_deleted_specific_key_misses_despite_a_kept_all_entry(tmp_path):
    # STRICT: the kept all-level entry cannot serve the rate the user
    # deleted — the marked miss forces the promised 0 instead.
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd", 175)
    store.set("dolbyvision|all|truehd", -25)
    store.delete("dolbyvision|23|truehd")
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                          per_fps=True)
    assert got.hit_kind == resolve.MISS
    assert got.entry is None
    assert got.reset_keys == ("dolbyvision|23|truehd",)


def test_unmarked_resolutions_carry_no_reset_keys(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd", 175)
    hit = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                          per_fps=False)
    assert hit.reset_keys == ()
    miss = resolve.resolve(store, "hdr10", 24.0, "ac3", per_fps=False)
    assert miss.reset_keys == ()


def test_dormant_marker_is_invisible_while_the_toggle_is_off(tmp_path):
    # A deleted per-fps key is not consulted with the toggle off — same
    # dormancy rule as the entries themselves, in both directions.
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd", 175)
    store.delete("dolbyvision|23|truehd")
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                          per_fps=False)
    assert got.reset_keys == ()
