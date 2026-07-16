"""The key-schema decision table, row by row (EVOLVED design §3.2).

Every behavior here is a decided rule, not an implementation detail:
lookup order per toggle state, dormancy in both directions, miss = None,
and the D4 write rule (store-instant derivation, never lookup-dependent).
"""

import pytest

from resources.lib.aom.store import resolve
from resources.lib.aom.store.offset_store import OffsetStore


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


# --- toggle ON: exact -> all -> miss -----------------------------------------

def test_on_exact_hit(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd", 175)
    store.set("dolbyvision|all|truehd", 100)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", per_fps=True)
    assert got.entry["delay_ms"] == 175
    assert got.hit_kind == resolve.EXACT
    assert got.key == "dolbyvision|23|truehd"


def test_on_falls_back_to_the_all_key(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd", 100)
    got = resolve.resolve(store, "dolbyvision", 60.0, "truehd", per_fps=True)
    assert got.entry["delay_ms"] == 100
    assert got.hit_kind == resolve.FALLBACK
    assert got.key == "dolbyvision|all|truehd"


def test_on_miss_when_neither_level_exists(tmp_path):
    # A specific entry for a DIFFERENT fps does not fall back sideways:
    # both lookup levels are single keys, so 23's entry cannot serve 60.
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd", 175)
    got = resolve.resolve(store, "dolbyvision", 60.0, "truehd", per_fps=True)
    assert got.entry is None
    assert got.hit_kind == resolve.MISS
    assert got.key is None
    # The whole consulted chain is visible for the debug line.
    assert got.tried == ("dolbyvision|60|truehd", "dolbyvision|all|truehd")


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


# --- toggle flips are non-destructive ----------------------------------------

def test_flip_off_then_on_reaches_the_all_entry_as_fallback(tmp_path):
    store = make_store(tmp_path)
    store.set(resolve.write_key("dolbyvision", None, "truehd", per_fps=False), 100)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", per_fps=True)
    assert got.entry["delay_ms"] == 100
    assert got.hit_kind == resolve.FALLBACK


def test_flip_on_then_off_leaves_specific_dormant_but_stored(tmp_path):
    store = make_store(tmp_path)
    key = resolve.write_key("dolbyvision", 23.976, "truehd", per_fps=True)
    store.set(key, 175)
    assert resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                           per_fps=False).hit_kind == resolve.MISS
    # Still in the store (management view sees it; ON reaches it again).
    assert store.get(key)["delay_ms"] == 175


# --- the D4 write rule ---------------------------------------------------------

def test_write_key_off_targets_the_all_key(tmp_path):
    assert resolve.write_key("DolbyVision", 23.976, "TrueHD",
                             per_fps=False) == "dolbyvision|all|truehd"


def test_write_key_on_targets_the_specific_key(tmp_path):
    assert resolve.write_key("DolbyVision", 23.976, "TrueHD",
                             per_fps=True) == "dolbyvision|23|truehd"


def test_write_then_resolve_roundtrips_as_exact(tmp_path):
    # The worked flow from the design doc: teach at all-level, fall back at
    # 60, nudge writes the specific key, 60 then hits exact while 24 still
    # falls back to the all entry.
    store = make_store(tmp_path)
    store.set(resolve.write_key("dv", None, "truehd", per_fps=False), 175)

    at60 = resolve.resolve(store, "dv", 60.0, "truehd", per_fps=True)
    assert (at60.hit_kind, at60.entry["delay_ms"]) == (resolve.FALLBACK, 175)

    store.set(resolve.write_key("dv", 60.0, "truehd", per_fps=True), 120)
    at60 = resolve.resolve(store, "dv", 60.0, "truehd", per_fps=True)
    assert (at60.hit_kind, at60.entry["delay_ms"]) == (resolve.EXACT, 120)
    at24 = resolve.resolve(store, "dv", 24.0, "truehd", per_fps=True)
    assert (at24.hit_kind, at24.entry["delay_ms"]) == (resolve.FALLBACK, 175)


# --- open vocabulary flows straight through ------------------------------------

def test_unheard_of_formats_resolve_like_any_other(tmp_path):
    store = make_store(tmp_path)
    key = resolve.write_key("HDR10+", 119.88, "x-future-codec", per_fps=True)
    assert key == "hdr10+|119|x-future-codec"
    store.set(key, -115)
    got = resolve.resolve(store, "HDR10+", 119.88, "x-future-codec", per_fps=True)
    assert (got.hit_kind, got.entry["delay_ms"]) == (resolve.EXACT, -115)


def test_on_with_unparseable_fps_degrades_to_the_fallback_level(tmp_path):
    # resolve() is total: an fps that cannot be parsed means the exact LEVEL
    # is unavailable, not that lookup should explode — the all key still
    # applies (a benign miss must never become an exception in the apply
    # path).
    store = make_store(tmp_path)
    store.set("sdr|all|aac", -50)
    got = resolve.resolve(store, "sdr", "abc", "aac", per_fps=True)
    assert got.entry["delay_ms"] == -50
    assert got.hit_kind == resolve.FALLBACK
    assert got.tried == ("sdr|all|aac",)  # exact level never composed


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
    # Consumers read .ms instead of indexing entry['delay_ms'] (E2 review:
    # the entry dict shape stays inside the store package).
    store = make_store(tmp_path)
    store.set("sdr|all|aac", -115)
    hit = resolve.resolve(store, "sdr", None, "aac", per_fps=False)
    assert hit.ms == -115
    miss = resolve.resolve(store, "sdr", None, "flac", per_fps=False)
    assert miss.ms is None

