"""Behavioral tests for aome.store.table (the OffsetTable adapter).

Moved here from test_kodi_settings.py with the class itself:
the table adapts the pure store + one injected settings read, so it lives
in the store package. Uses a REAL OffsetStore on tmp_path — only the
settings toggle is faked.
"""

from resources.lib.aome.domain.profile import StreamProfile
from resources.lib.aome.store.offset_store import OffsetStore
from resources.lib.aome.store.table import OffsetTable


def _profile(hdr='dolbyvision', audio='truehd', video_fps=23.976, channels=8):
    return StreamProfile(
        hdr_type=hdr,
        audio_format=audio,
        video_fps=video_fps,
        player_id=1,
        audio_channels=channels,
    )


class _ToggleSettings:
    """Just the granularity reads the adapter consults — flipped between calls to
    prove keys are composed at CALL TIME, never captured."""

    def __init__(self, per_fps=False, distinct_spatial=True,
                 distinct_channels=False):
        self.per_fps = per_fps
        self.distinct_spatial = distinct_spatial
        self.distinct_channels = distinct_channels

    def per_fps_offsets_enabled(self):
        return self.per_fps

    def distinct_spatial_enabled(self):
        return self.distinct_spatial

    def distinct_channels_enabled(self):
        return self.distinct_channels


def _make_table(tmp_path, per_fps=False, distinct_spatial=True,
                distinct_channels=False):
    from resources.lib.aome.store.offset_store import OffsetStore
    store = OffsetStore(str(tmp_path / "offsets.json"))
    store.load()
    return OffsetTable(store,
                       _ToggleSettings(per_fps, distinct_spatial,
                                       distinct_channels)), store


class TestOffsetTable:
    def test_store_writes_the_d4_key_with_fps_metadata(self, tmp_path):
        table, store = _make_table(tmp_path, per_fps=True)
        profile = _profile(hdr='hdr10', audio='eac3', video_fps=23.976)
        assert table.store(profile, -115) == 'hdr10|23|eac3|all'
        entry = store.get('hdr10|23|eac3|all')
        assert entry['delay_ms'] == -115           # verbatim
        assert entry['video_fps'] == 23.976        # management-view metadata

    def test_resolve_consults_only_the_mode_key(self, tmp_path):
        # STRICT: per_fps ON never falls back to the all entry — the one
        # candidate key either hits or the resolution is a miss.
        table, store = _make_table(tmp_path, per_fps=True)
        store.set('hdr10|all|eac3|all', 100)
        got = table.resolve(_profile(hdr='hdr10', audio='eac3',
                                     video_fps=60.0))
        assert (got.entry, got.hit_kind, got.key) == (None, 'miss', None)
        assert got.tried == ('hdr10|60|eac3|all',)

    def test_keys_are_composed_at_call_time_from_the_live_toggle(self, tmp_path):
        # Freshness doctrine: the SAME profile writes different keys as the
        # toggle changes between calls — nothing is captured.
        table, store = _make_table(tmp_path, per_fps=False)
        profile = _profile(hdr='sdr', audio='ac3', video_fps=50.0)
        assert table.store(profile, 25) == 'sdr|all|ac3|all'
        table._settings.per_fps = True
        assert table.store(profile, 40) == 'sdr|50|ac3|all'
        assert store.get('sdr|all|ac3|all')['delay_ms'] == 25   # untouched

    def test_miss_resolution_is_a_no_entry_answer(self, tmp_path):
        table, _store = _make_table(tmp_path)
        got = table.resolve(_profile())
        assert (got.entry, got.hit_kind, got.key) == (None, 'miss', None)

    def test_write_key_is_none_when_uncomposable(self, tmp_path):
        table, _store = _make_table(tmp_path, per_fps=True)
        assert table.write_key(_profile(video_fps=None)) is None

    def test_get_at_reads_exact_keys_only(self, tmp_path):
        table, store = _make_table(tmp_path, per_fps=True)
        store.set('hdr10|all|eac3|all', 100)
        assert table.get_at('hdr10|all|eac3|all')['delay_ms'] == 100
        assert table.get_at('hdr10|23|eac3|all') is None  # no fallback here

    def test_stored_ms_at_and_read_only_passthrough(self, tmp_path):
        table, store = _make_table(tmp_path)
        store.set('sdr|all|aac|all', -115)
        assert table.stored_ms_at('sdr|all|aac|all') == -115
        assert table.stored_ms_at('sdr|all|flac|all') is None
        assert table.read_only is False


class TestSpatialToggle:
    def test_keys_are_composed_at_call_time_from_the_live_spatial_toggle(
            self, tmp_path):
        # Freshness doctrine, spatial axis: the SAME variant profile writes
        # different keys as the toggle changes between calls.
        table, store = _make_table(tmp_path, distinct_spatial=True)
        profile = _profile(audio='truehd_atmos')
        assert table.store(profile, 25) == 'dolbyvision|all|truehd_atmos|all'
        table._settings.distinct_spatial = False
        assert table.store(profile, 40) == 'dolbyvision|all|truehd|all'
        assert store.get('dolbyvision|all|truehd_atmos|all')['delay_ms'] == 25

    def test_resolve_collapses_the_variant_with_distinct_off(self, tmp_path):
        table, store = _make_table(tmp_path, distinct_spatial=False)
        store.set('dolbyvision|all|truehd|all', 175)
        got = table.resolve(_profile(audio='truehd_atmos'))
        assert (got.hit_kind, got.key, got.ms) == (
            'exact', 'dolbyvision|all|truehd|all', 175)


class TestChannelToggle:
    def test_keys_are_composed_at_call_time_from_the_live_channel_toggle(
            self, tmp_path):
        # Freshness doctrine, channel axis: the SAME profile writes
        # different keys as the toggle changes between calls.
        table, store = _make_table(tmp_path, distinct_channels=False)
        profile = _profile(channels=8)
        assert table.store(profile, 25) == 'dolbyvision|all|truehd|all'
        table._settings.distinct_channels = True
        assert table.store(profile, 40) == 'dolbyvision|all|truehd|8'
        assert store.get('dolbyvision|all|truehd|all')['delay_ms'] == 25

    def test_resolve_consults_only_the_count_key_with_distinct_on(
            self, tmp_path):
        table, store = _make_table(tmp_path, distinct_channels=True)
        store.set('dolbyvision|all|truehd|all', 175)
        got = table.resolve(_profile(channels=8))
        assert (got.entry, got.hit_kind) == (None, 'miss')
        assert got.tried == ('dolbyvision|all|truehd|8',)

    def test_profile_without_a_usable_count_degrades_to_the_all_key(
            self, tmp_path):
        # The incidental field can be 'unknown' on a gather the gateway
        # could not complete: the candidate (and write key) degrade to the
        # all-channels key rather than raising or stranding a value.
        table, store = _make_table(tmp_path, distinct_channels=True)
        store.set('dolbyvision|all|truehd|all', 175)
        got = table.resolve(_profile(channels='unknown'))
        assert (got.hit_kind, got.ms) == ('exact', 175)
        assert table.store(_profile(channels='unknown'), 30) == \
            'dolbyvision|all|truehd|all'

    def test_all_three_toggles_compose(self, tmp_path):
        table, _store = _make_table(tmp_path, per_fps=True,
                                    distinct_spatial=False,
                                    distinct_channels=True)
        profile = _profile(audio='truehd_atmos', video_fps=23.976, channels=8)
        assert table.store(profile, -10) == 'dolbyvision|23|truehd|8'
