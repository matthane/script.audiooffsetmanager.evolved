"""Behavioral tests for aome.store.table (the OffsetTable adapter).

Moved here from test_kodi_settings.py with the class itself:
the table adapts the pure store + one injected settings read, so it lives
in the store package. Uses a REAL OffsetStore on tmp_path — only the
settings toggle is faked.
"""

from resources.lib.aome.domain.profile import StreamProfile
from resources.lib.aome.store.offset_store import OffsetStore
from resources.lib.aome.store.table import OffsetTable


def _profile(hdr='dolbyvision', audio='truehd', video_fps=23.976):
    return StreamProfile(
        hdr_type=hdr,
        audio_format=audio,
        video_fps=video_fps,
        player_id=1,
        audio_channels=8,
    )


class _ToggleSettings:
    """Just the per_fps read the adapter consults — flipped between calls to
    prove keys are composed at CALL TIME, never captured."""

    def __init__(self, per_fps=False):
        self.per_fps = per_fps

    def per_fps_offsets_enabled(self):
        return self.per_fps


def _make_table(tmp_path, per_fps=False):
    from resources.lib.aome.store.offset_store import OffsetStore
    store = OffsetStore(str(tmp_path / "offsets.json"))
    store.load()
    return OffsetTable(store, _ToggleSettings(per_fps)), store


class TestOffsetTable:
    def test_store_writes_the_d4_key_with_fps_metadata(self, tmp_path):
        table, store = _make_table(tmp_path, per_fps=True)
        profile = _profile(hdr='hdr10', audio='eac3', video_fps=23.976)
        assert table.store(profile, -115) == 'hdr10|23|eac3'
        entry = store.get('hdr10|23|eac3')
        assert entry['delay_ms'] == -115           # verbatim
        assert entry['video_fps'] == 23.976        # management-view metadata

    def test_resolve_walks_the_chain_and_reports_hit_kind(self, tmp_path):
        table, store = _make_table(tmp_path, per_fps=True)
        store.set('hdr10|all|eac3', 100)
        got = table.resolve(_profile(hdr='hdr10', audio='eac3',
                                     video_fps=60.0))
        assert got.entry['delay_ms'] == 100
        assert got.hit_kind == 'fallback'
        assert got.tried == ('hdr10|60|eac3', 'hdr10|all|eac3')

    def test_keys_are_composed_at_call_time_from_the_live_toggle(self, tmp_path):
        # Freshness doctrine: the SAME profile writes different keys as the
        # toggle changes between calls — nothing is captured.
        table, store = _make_table(tmp_path, per_fps=False)
        profile = _profile(hdr='sdr', audio='ac3', video_fps=50.0)
        assert table.store(profile, 25) == 'sdr|all|ac3'
        table._settings.per_fps = True
        assert table.store(profile, 40) == 'sdr|50|ac3'
        assert store.get('sdr|all|ac3')['delay_ms'] == 25   # untouched

    def test_miss_resolution_is_a_no_entry_answer(self, tmp_path):
        table, _store = _make_table(tmp_path)
        got = table.resolve(_profile())
        assert (got.entry, got.hit_kind, got.key) == (None, 'miss', None)

    def test_write_key_is_none_when_uncomposable(self, tmp_path):
        table, _store = _make_table(tmp_path, per_fps=True)
        assert table.write_key(_profile(video_fps=None)) is None

    def test_get_at_reads_exact_keys_only(self, tmp_path):
        table, store = _make_table(tmp_path, per_fps=True)
        store.set('hdr10|all|eac3', 100)
        assert table.get_at('hdr10|all|eac3')['delay_ms'] == 100
        assert table.get_at('hdr10|23|eac3') is None  # no fallback here

    def test_stored_ms_at_and_read_only_passthrough(self, tmp_path):
        table, store = _make_table(tmp_path)
        store.set('sdr|all|aac', -115)
        assert table.stored_ms_at('sdr|all|aac') == -115
        assert table.stored_ms_at('sdr|all|flac') is None
        assert table.read_only is False

