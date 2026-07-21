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
    store.set("dolbyvision|all|truehd|all", 175)
    got = resolve.resolve(store, "DolbyVision", 23.976, "TrueHD", per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert got.entry["delay_ms"] == 175
    assert got.hit_kind == resolve.EXACT
    assert got.key == "dolbyvision|all|truehd|all"


def test_off_empty_store_is_a_miss(tmp_path):
    store = make_store(tmp_path)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert got.entry is None
    assert got.hit_kind == resolve.MISS
    assert got.key is None                       # stable meaning: no hit
    assert got.tried == ("dolbyvision|all|truehd|all",)


def test_off_specific_entries_are_dormant(tmp_path):
    # A specific-fps entry taught while the toggle was ON is never matched
    # while it is OFF — dormant, not deleted (decision table row).
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd|all", 175)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert got.hit_kind == resolve.MISS


def test_off_ignores_the_fps_value_entirely(tmp_path):
    # With the toggle off the fps argument must not even be parsed —
    # None/garbage flows through because the axis does not exist.
    store = make_store(tmp_path)
    store.set("sdr|all|aac|all", -50)
    got = resolve.resolve(store, "sdr", None, "aac", per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert got.entry["delay_ms"] == -50


# --- toggle ON: the specific key or nothing ----------------------------------

def test_on_exact_hit(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd|all", 175)
    store.set("dolbyvision|all|truehd|all", 100)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert got.entry["delay_ms"] == 175
    assert got.hit_kind == resolve.EXACT
    assert got.key == "dolbyvision|23|truehd|all"


def test_on_all_entries_are_dormant(tmp_path):
    # STRICT: an offset saved while the toggle was off covers all frame
    # rates and is NOT applied while the toggle is on — no fallback level
    # exists. Dormant, not deleted (the mirror of the OFF-side dormancy
    # row above).
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|all", 100)
    got = resolve.resolve(store, "dolbyvision", 60.0, "truehd", per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert got.entry is None
    assert got.hit_kind == resolve.MISS
    assert got.key is None
    assert got.tried == ("dolbyvision|60|truehd|all",)  # the all key: not consulted


def test_on_miss_for_an_untaught_rate(tmp_path):
    # A specific entry for a DIFFERENT fps does not serve sideways:
    # the candidate is a single key, so 23's entry cannot serve 60.
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd|all", 175)
    got = resolve.resolve(store, "dolbyvision", 60.0, "truehd", per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert got.entry is None
    assert got.hit_kind == resolve.MISS
    assert got.tried == ("dolbyvision|60|truehd|all",)


def test_on_fractional_rates_resolve_their_own_keys(tmp_path):
    # 23.976 and 24.0 are different HDMI modes with different latencies:
    # distinct keys, independent values (design guarantee).
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd|all", 175)
    store.set("dolbyvision|24|truehd|all", 120)
    got23 = resolve.resolve(store, "dolbyvision", 23.976, "truehd", per_fps=True, distinct_spatial=True, distinct_channels=False)
    got24 = resolve.resolve(store, "dolbyvision", 24.0, "truehd", per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert got23.entry["delay_ms"] == 175
    assert got24.entry["delay_ms"] == 120


# --- toggle flips are non-destructive, dormancy is symmetric ------------------

def test_flip_off_then_on_leaves_all_dormant_but_stored(tmp_path):
    store = make_store(tmp_path)
    key = resolve.write_key("dolbyvision", None, "truehd", per_fps=False, distinct_spatial=True, distinct_channels=False)
    store.set(key, 100)
    assert resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                           per_fps=True, distinct_spatial=True, distinct_channels=False).hit_kind == resolve.MISS
    # Still in the store (management view sees it; OFF reaches it again).
    assert store.get(key)["delay_ms"] == 100
    assert resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                           per_fps=False, distinct_spatial=True, distinct_channels=False).entry["delay_ms"] == 100


def test_flip_on_then_off_leaves_specific_dormant_but_stored(tmp_path):
    store = make_store(tmp_path)
    key = resolve.write_key("dolbyvision", 23.976, "truehd", per_fps=True, distinct_spatial=True, distinct_channels=False)
    store.set(key, 175)
    assert resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                           per_fps=False, distinct_spatial=True, distinct_channels=False).hit_kind == resolve.MISS
    # Still in the store (management view sees it; ON reaches it again).
    assert store.get(key)["delay_ms"] == 175
    assert resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                           per_fps=True, distinct_spatial=True, distinct_channels=False).entry["delay_ms"] == 175


# --- the write rule ---------------------------------------------------------

def test_write_key_off_targets_the_all_key(tmp_path):
    assert resolve.write_key("DolbyVision", 23.976, "TrueHD",
                             per_fps=False, distinct_spatial=True, distinct_channels=False) == "dolbyvision|all|truehd|all"


def test_write_key_on_targets_the_specific_key(tmp_path):
    assert resolve.write_key("DolbyVision", 23.976, "TrueHD",
                             per_fps=True, distinct_spatial=True, distinct_channels=False) == "dolbyvision|23|truehd|all"


def test_write_then_resolve_roundtrips_as_exact(tmp_path):
    # The re-teach flow: an all-level offset taught with the toggle off is
    # dormant once it is on; teaching 60 writes the specific key and 60
    # hits exact, while 24 stays a miss until taught itself.
    store = make_store(tmp_path)
    store.set(resolve.write_key("dv", None, "truehd", per_fps=False, distinct_spatial=True, distinct_channels=False), 175)

    at60 = resolve.resolve(store, "dv", 60.0, "truehd", per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert (at60.hit_kind, at60.entry) == (resolve.MISS, None)

    store.set(resolve.write_key("dv", 60.0, "truehd", per_fps=True, distinct_spatial=True, distinct_channels=False), 120)
    at60 = resolve.resolve(store, "dv", 60.0, "truehd", per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert (at60.hit_kind, at60.entry["delay_ms"]) == (resolve.EXACT, 120)
    at24 = resolve.resolve(store, "dv", 24.0, "truehd", per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert (at24.hit_kind, at24.entry) == (resolve.MISS, None)


# --- open vocabulary flows straight through ------------------------------------

def test_unheard_of_formats_resolve_like_any_other(tmp_path):
    store = make_store(tmp_path)
    key = resolve.write_key("HDR10+", 119.88, "x-future-codec", per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert key == "hdr10plus|119|x-future-codec|all"
    store.set(key, -115)
    got = resolve.resolve(store, "HDR10+", 119.88, "x-future-codec", per_fps=True, distinct_spatial=True, distinct_channels=False)
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
    got = resolve.resolve(store, "HDR10+", 23.976, "TrueHD", per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert (got.hit_kind, got.entry["delay_ms"]) == (resolve.EXACT, -40)
    got = resolve.resolve(store, "Dolby Vision", 23.976, "truehd_atmos",
                          per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert (got.hit_kind, got.entry["delay_ms"]) == (resolve.EXACT, 50)


def test_on_with_unparseable_fps_consults_the_all_key(tmp_path):
    # resolve() is total: an fps that cannot be parsed means the stream
    # has NO fps axis, so the all key IS its exact key — the same meaning
    # 'all' carries with the toggle off (a benign miss must never become
    # an exception in the apply path). Defensive only: completeness
    # gating keeps unparseable rates out of the production apply path.
    store = make_store(tmp_path)
    store.set("sdr|all|aac|all", -50)
    got = resolve.resolve(store, "sdr", "abc", "aac", per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert got.entry["delay_ms"] == -50
    assert got.hit_kind == resolve.EXACT
    assert got.tried == ("sdr|all|aac|all",)  # the specific level never composed


def test_on_with_unparseable_fps_and_empty_store_is_a_miss(tmp_path):
    store = make_store(tmp_path)
    got = resolve.resolve(store, "sdr", None, "aac", per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert (got.entry, got.hit_kind, got.key) == (None, resolve.MISS, None)
    assert got.tried == ("sdr|all|aac|all",)


def test_write_key_stays_strict_on_unparseable_fps(tmp_path):
    # The WRITE side keeps the loud contract: storing under a garbage key
    # is worse than failing — writers are gated on verified profiles.
    with pytest.raises(ValueError):
        resolve.write_key("sdr", "abc", "aac", per_fps=True, distinct_spatial=True, distinct_channels=False)


def test_resolution_ms_accessor_keeps_entry_shape_internal(tmp_path):
    # Consumers read .ms instead of indexing entry['delay_ms'] (the entry
    # dict shape stays inside the store package).
    store = make_store(tmp_path)
    store.set("sdr|all|aac|all", -115)
    hit = resolve.resolve(store, "sdr", None, "aac", per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert hit.ms == -115
    miss = resolve.resolve(store, "sdr", None, "flac", per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert miss.ms is None


# --- reset markers on the consulted key -----------

def test_marked_miss_carries_the_reset_key(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|all", 175)
    store.delete("dolbyvision|all|truehd|all")
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                          per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert got.hit_kind == resolve.MISS
    assert got.reset_keys == ("dolbyvision|all|truehd|all",)


def test_on_marked_miss_carries_the_specific_key_only(tmp_path):
    # Only the consulted key's marker travels: the deleted all-level
    # marker is dormant while the toggle is on, exactly like the entries.
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd|all", 175)
    store.set("dolbyvision|all|truehd|all", -25)
    store.delete("dolbyvision|23|truehd|all")
    store.delete("dolbyvision|all|truehd|all")
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                          per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert got.hit_kind == resolve.MISS
    assert got.reset_keys == ("dolbyvision|23|truehd|all",)


def test_on_deleted_specific_key_misses_despite_a_kept_all_entry(tmp_path):
    # STRICT: the kept all-level entry cannot serve the rate the user
    # deleted — the marked miss forces the promised 0 instead.
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd|all", 175)
    store.set("dolbyvision|all|truehd|all", -25)
    store.delete("dolbyvision|23|truehd|all")
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                          per_fps=True, distinct_spatial=True, distinct_channels=False)
    assert got.hit_kind == resolve.MISS
    assert got.entry is None
    assert got.reset_keys == ("dolbyvision|23|truehd|all",)


def test_unmarked_resolutions_carry_no_reset_keys(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|all", 175)
    hit = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                          per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert hit.reset_keys == ()
    miss = resolve.resolve(store, "hdr10", 24.0, "ac3", per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert miss.reset_keys == ()


def test_dormant_marker_is_invisible_while_the_toggle_is_off(tmp_path):
    # A deleted per-fps key is not consulted with the toggle off — same
    # dormancy rule as the entries themselves, in both directions.
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd|all", 175)
    store.delete("dolbyvision|23|truehd|all")
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                          per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert got.reset_keys == ()


# --- distinct_spatial OFF: the audio axis collapses to the base codec --------

def test_spatial_off_consults_the_base_key(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|all", 175)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd_atmos",
                          per_fps=False, distinct_spatial=False, distinct_channels=False)
    assert (got.hit_kind, got.key, got.ms) == (
        resolve.EXACT, "dolbyvision|all|truehd|all", 175)
    assert got.tried == ("dolbyvision|all|truehd|all",)


def test_spatial_off_never_reads_the_variant_entry(tmp_path):
    # One candidate per call: the variant's own entry is dormant while the
    # toggle is off, even when it is the only entry in the store.
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd_atmos|all", -125)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd_atmos",
                          per_fps=False, distinct_spatial=False, distinct_channels=False)
    assert (got.entry, got.hit_kind) == (None, resolve.MISS)
    assert got.tried == ("dolbyvision|all|truehd|all",)


def test_spatial_on_never_reads_the_base_entry_for_a_variant(tmp_path):
    # Strict in the other direction too: with distinct on, a variant
    # stream consults only its own verbatim key.
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|all", 175)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd_atmos",
                          per_fps=False, distinct_spatial=True, distinct_channels=False)
    assert (got.entry, got.hit_kind) == (None, resolve.MISS)
    assert got.tried == ("dolbyvision|all|truehd_atmos|all",)


def test_base_key_is_live_in_both_spatial_modes(tmp_path):
    # The one-sided rule: an entry under the base codec's key serves plain
    # streams identically whichever way the toggle points.
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|all", 175)
    for distinct in (False, True):
        got = resolve.resolve(store, "dolbyvision", 23.976, "truehd",
                              per_fps=False, distinct_spatial=distinct,
                              distinct_channels=False)
        assert (got.hit_kind, got.ms) == (resolve.EXACT, 175)


def test_spatial_off_composes_with_per_fps_on(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd|all", -50)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd_atmos",
                          per_fps=True, distinct_spatial=False, distinct_channels=False)
    assert (got.hit_kind, got.key) == (resolve.EXACT, "dolbyvision|23|truehd|all")


def test_write_key_collapses_with_distinct_off():
    assert resolve.write_key("dolbyvision", 23.976, "truehd_atmos",
                             per_fps=False, distinct_spatial=False, distinct_channels=False) \
        == "dolbyvision|all|truehd|all"
    assert resolve.write_key("dolbyvision", 23.976, "truehd_atmos",
                             per_fps=True, distinct_spatial=False, distinct_channels=False) \
        == "dolbyvision|23|truehd|all"


def test_spatial_off_sees_the_base_keys_reset_marker(tmp_path):
    # A deleted base entry promises 0 to every stream that resolves to it,
    # including a variant stream while the toggle is off.
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|all", 175)
    store.delete("dolbyvision|all|truehd|all")
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd_atmos",
                          per_fps=False, distinct_spatial=False, distinct_channels=False)
    assert got.hit_kind == resolve.MISS
    assert got.reset_keys == ("dolbyvision|all|truehd|all",)


# --- distinct_channels: the channel axis, symmetric like fps ------------------

def test_channels_off_hits_the_all_key_and_ignores_the_count(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|all", 175)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", 8,
                          per_fps=False, distinct_spatial=True,
                          distinct_channels=False)
    assert (got.hit_kind, got.key, got.ms) == (
        resolve.EXACT, "dolbyvision|all|truehd|all", 175)


def test_channels_on_consults_only_the_count_key(tmp_path):
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|8", -30)
    store.set("dolbyvision|all|truehd|all", 175)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", 8,
                          per_fps=False, distinct_spatial=True,
                          distinct_channels=True)
    assert (got.hit_kind, got.key, got.ms) == (
        resolve.EXACT, "dolbyvision|all|truehd|8", -30)
    assert got.tried == ("dolbyvision|all|truehd|8",)


def test_channels_on_all_entries_are_dormant(tmp_path):
    # STRICT, mirroring per_fps: an offset saved while the toggle was off
    # covers all counts and is NOT applied while it is on — no fallback.
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|all", 175)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", 8,
                          per_fps=False, distinct_spatial=True,
                          distinct_channels=True)
    assert (got.entry, got.hit_kind, got.key) == (None, resolve.MISS, None)
    assert got.tried == ("dolbyvision|all|truehd|8",)


def test_channels_off_count_entries_are_dormant(tmp_path):
    # The mirror row: a count-specific entry sleeps while the toggle is
    # off. Dormant, not deleted.
    store = make_store(tmp_path)
    store.set("dolbyvision|all|truehd|8", -30)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd", 8,
                          per_fps=False, distinct_spatial=True,
                          distinct_channels=False)
    assert (got.entry, got.hit_kind) == (None, resolve.MISS)
    assert got.tried == ("dolbyvision|all|truehd|all",)
    assert store.get("dolbyvision|all|truehd|8")["delay_ms"] == -30


def test_channels_on_different_counts_resolve_their_own_keys(tmp_path):
    # The feature's promise: 5.1 and stereo variants of one codec carry
    # independent values.
    store = make_store(tmp_path)
    store.set("sdr|all|flac|2", 10)
    store.set("sdr|all|flac|6", 85)
    got2 = resolve.resolve(store, "sdr", 24.0, "flac", 2, per_fps=False,
                           distinct_spatial=True, distinct_channels=True)
    got6 = resolve.resolve(store, "sdr", 24.0, "flac", 6, per_fps=False,
                           distinct_spatial=True, distinct_channels=True)
    assert (got2.ms, got6.ms) == (10, 85)


def test_channels_on_with_unusable_count_consults_the_all_key(tmp_path):
    # Degradation is symmetric with the write rule: a channel-less stream's
    # candidate IS the all key, exactly as with the toggle off (handled in
    # keys.channel_segment; resolve never raises for channels).
    store = make_store(tmp_path)
    store.set("sdr|all|aac|all", -50)
    for bad in (None, 'unknown', 0):
        got = resolve.resolve(store, "sdr", 24.0, "aac", bad, per_fps=False,
                              distinct_spatial=True, distinct_channels=True)
        assert (got.hit_kind, got.ms) == (resolve.EXACT, -50)
        assert got.tried == ("sdr|all|aac|all",)


def test_write_key_channel_modes_and_degradation(tmp_path):
    assert resolve.write_key("dolbyvision", 23.976, "truehd", 8,
                             per_fps=False, distinct_spatial=True,
                             distinct_channels=True) == \
        "dolbyvision|all|truehd|8"
    assert resolve.write_key("dolbyvision", 23.976, "truehd", 8,
                             per_fps=False, distinct_spatial=True,
                             distinct_channels=False) == \
        "dolbyvision|all|truehd|all"
    # The write degrades exactly as lookup does — never a stranded key.
    assert resolve.write_key("dolbyvision", 23.976, "truehd", 'unknown',
                             per_fps=False, distinct_spatial=True,
                             distinct_channels=True) == \
        "dolbyvision|all|truehd|all"


def test_all_three_toggles_compose_one_candidate(tmp_path):
    # per_fps on + distinct_spatial off + distinct_channels on: one key,
    # each axis at its own granularity.
    store = make_store(tmp_path)
    store.set("dolbyvision|23|truehd|8", 55)
    got = resolve.resolve(store, "dolbyvision", 23.976, "truehd_atmos", 8,
                          per_fps=True, distinct_spatial=False,
                          distinct_channels=True)
    assert (got.hit_kind, got.key, got.ms) == (
        resolve.EXACT, "dolbyvision|23|truehd|8", 55)
    assert got.tried == ("dolbyvision|23|truehd|8",)


def test_channels_flip_is_non_destructive_both_ways(tmp_path):
    store = make_store(tmp_path)
    all_key = resolve.write_key("sdr", None, "flac", 6, per_fps=False,
                                distinct_spatial=True,
                                distinct_channels=False)
    count_key = resolve.write_key("sdr", None, "flac", 6, per_fps=False,
                                  distinct_spatial=True,
                                  distinct_channels=True)
    store.set(all_key, 100)
    store.set(count_key, -40)
    on = resolve.resolve(store, "sdr", None, "flac", 6, per_fps=False,
                         distinct_spatial=True, distinct_channels=True)
    off = resolve.resolve(store, "sdr", None, "flac", 6, per_fps=False,
                          distinct_spatial=True, distinct_channels=False)
    assert (on.ms, off.ms) == (-40, 100)
    assert store.get(all_key)["delay_ms"] == 100
    assert store.get(count_key)["delay_ms"] == -40


def test_channels_marked_miss_carries_the_consulted_key_only(tmp_path):
    # Only the consulted key's marker travels: the deleted count key's
    # marker is dormant while the toggle is off, like the entries.
    store = make_store(tmp_path)
    store.set("sdr|all|flac|6", 85)
    store.delete("sdr|all|flac|6")
    on = resolve.resolve(store, "sdr", None, "flac", 6, per_fps=False,
                         distinct_spatial=True, distinct_channels=True)
    assert (on.hit_kind, on.reset_keys) == (
        resolve.MISS, ("sdr|all|flac|6",))
    off = resolve.resolve(store, "sdr", None, "flac", 6, per_fps=False,
                          distinct_spatial=True, distinct_channels=False)
    assert (off.hit_kind, off.reset_keys) == (resolve.MISS, ())


def test_schema1_file_entries_resolve_after_load(tmp_path):
    # End-to-end migration guarantee: a version-1 offsets.json (3-segment
    # keys) resolves for today's 4-segment candidates because load()
    # expands keys at the store boundary.
    import json
    path = tmp_path / "offsets.json"
    path.write_text(json.dumps({"version": 1, "profiles": {
        "dolbyvision|all|truehd_atmos": {"delay_ms": 40},
        "hdr10+|23|ac3": {"delay_ms": -65},
    }}), encoding="utf-8")
    store = OffsetStore(str(path))
    store.load()
    got = resolve.resolve(store, "Dolby Vision", 23.976, "truehd_atmos",
                          per_fps=False, distinct_spatial=True,
                          distinct_channels=False)
    assert (got.hit_kind, got.ms) == (resolve.EXACT, 40)
    got = resolve.resolve(store, "HDR10+", 23.976, "ac3", 6, per_fps=True,
                          distinct_spatial=True, distinct_channels=False)
    assert (got.hit_kind, got.ms) == (resolve.EXACT, -65)
