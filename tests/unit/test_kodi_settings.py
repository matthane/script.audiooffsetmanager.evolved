"""Behavioral tests for aom.kodi.settings (Settings + OffsetTable).

Kodi is faked via Kodistubs: ``xbmcaddon.Addon(...).getSettings()`` yields a
stub ``Settings`` object whose ``getBool``/``getInt``/``setBool``/``setInt`` we
spy or monkeypatch per test. Log-sink calls are collected in a list of
``(message, level)`` tuples so assertions can pin both the message text and the
Kodi log level. No fixtures beyond ``monkeypatch``.
"""

import xbmc

from resources.lib.aom.kodi.settings import ADDON_ID, Settings, OffsetTable
from resources.lib.aom.domain.profile import StreamProfile


# --- helpers -----------------------------------------------------------------

def _make_settings():
    """Build a Settings with a list-collecting log sink. Returns (settings, logs)."""
    logs = []
    settings = Settings(log=lambda message, level=None: logs.append((message, level)))
    return settings, logs


def _profile(hdr='dolbyvision', audio='truehd', video_fps=23.976):
    return StreamProfile(
        hdr_type=hdr,
        audio_format=audio,
        video_fps=video_fps,
        player_id=1,
        audio_channels=8,
    )


class _Spy:
    """Records calls; returns ``result`` or raises ``raises`` when invoked."""

    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return self.result


def test_addon_id_constant():
    assert ADDON_ID == 'script.audiooffsetmanagerevolved'


def test_settings_keeps_parent_addon_alive():
    """Regression pin for the 2.0.0~beta1 field bug (Kodi 21.2/Windows).

    ``xbmcaddon.Addon(...).getSettings()`` as a one-liner leaves the Addon a
    garbage-collected temporary, which orphans the Settings proxy: writes
    report success but never persist (``new_install`` clear was lost) and
    dialog edits never arrive. The proxy is live only while its parent Addon
    is alive, so Settings must hold the Addon it derived ``_settings`` from.
    """
    import xbmcaddon

    settings, _ = _make_settings()
    assert isinstance(settings._addon, xbmcaddon.Addon)


# --- get_bool / get_int ------------------------------------------------------

class TestTypedReads:
    def test_get_bool_normal_read(self):
        settings, logs = _make_settings()
        settings._settings.getBool = _Spy(result=True)
        assert settings.get_bool('enable_notifications') is True
        assert logs == []

    def test_get_bool_exception_returns_default_and_warns(self):
        settings, logs = _make_settings()
        settings._settings.getBool = _Spy(raises=RuntimeError("boom"))
        assert settings.get_bool('enable_notifications', default=True) is True
        assert len(logs) == 1
        message, level = logs[0]
        assert level == xbmc.LOGWARNING
        assert "Error getting boolean setting 'enable_notifications'" in message
        assert "Using default: True" in message

    def test_get_bool_default_is_false(self):
        settings, logs = _make_settings()
        settings._settings.getBool = _Spy(raises=ValueError())
        assert settings.get_bool('missing') is False

    def test_get_int_normal_read(self):
        settings, logs = _make_settings()
        settings._settings.getInt = _Spy(result=7)
        assert settings.get_int('notification_seconds') == 7
        assert logs == []

    def test_get_int_exception_returns_default_and_warns(self):
        settings, logs = _make_settings()
        settings._settings.getInt = _Spy(raises=RuntimeError("boom"))
        assert settings.get_int('notification_seconds', default=3) == 3
        assert len(logs) == 1
        message, level = logs[0]
        assert level == xbmc.LOGWARNING
        assert "Error getting integer setting 'notification_seconds'" in message
        assert "Using default: 3" in message

    def test_get_int_default_is_zero(self):
        settings, logs = _make_settings()
        settings._settings.getInt = _Spy(raises=ValueError())
        assert settings.get_int('missing') == 0


# --- store_*_if_changed ------------------------------------------------------

class TestStoreBooleanIfChanged:
    def test_unchanged_skips_write_returns_true(self):
        settings, logs = _make_settings()
        settings._settings.getBool = _Spy(result=True)
        set_spy = _Spy()
        settings._settings.setBool = set_spy
        assert settings.store_boolean_if_changed('pause_offsets', True) is True
        assert set_spy.calls == []
        assert logs == []

    def test_changed_writes_returns_true_and_logs_debug(self):
        settings, logs = _make_settings()
        settings._settings.getBool = _Spy(result=False)
        set_spy = _Spy()
        settings._settings.setBool = set_spy
        assert settings.store_boolean_if_changed('pause_offsets', True) is True
        assert set_spy.calls == [('pause_offsets', True)]
        assert len(logs) == 1
        message, level = logs[0]
        assert level == xbmc.LOGDEBUG
        assert "Storing boolean setting pause_offsets: True" in message

    def test_write_raises_returns_false_and_warns(self):
        settings, logs = _make_settings()
        settings._settings.getBool = _Spy(result=False)
        settings._settings.setBool = _Spy(raises=RuntimeError("no dice"))
        assert settings.store_boolean_if_changed('pause_offsets', True) is False
        # LOGDEBUG "Storing" then LOGWARNING "Error storing".
        levels = [level for _, level in logs]
        assert levels == [xbmc.LOGDEBUG, xbmc.LOGWARNING]
        assert "Error storing boolean setting 'pause_offsets'" in logs[-1][0]

    def test_pre_read_raising_falls_through_to_write(self):
        settings, logs = _make_settings()
        # get_bool swallows the read error -> default False; value True differs,
        # so the store falls through to the write attempt (like legacy).
        settings._settings.getBool = _Spy(raises=RuntimeError("read down"))
        set_spy = _Spy()
        settings._settings.setBool = set_spy
        assert settings.store_boolean_if_changed('pause_offsets', True) is True
        assert set_spy.calls == [('pause_offsets', True)]


class TestStoreIntegerIfChanged:
    def test_unchanged_skips_write_returns_true(self):
        settings, logs = _make_settings()
        settings._settings.getInt = _Spy(result=250)
        set_spy = _Spy()
        settings._settings.setInt = set_spy
        assert settings.store_integer_if_changed('notification_seconds', 250) is True
        assert set_spy.calls == []
        assert logs == []

    def test_changed_writes_returns_true_and_logs_debug(self):
        settings, logs = _make_settings()
        settings._settings.getInt = _Spy(result=100)
        set_spy = _Spy()
        settings._settings.setInt = set_spy
        assert settings.store_integer_if_changed('notification_seconds', 250) is True
        assert set_spy.calls == [('notification_seconds', 250)]
        assert len(logs) == 1
        message, level = logs[0]
        assert level == xbmc.LOGDEBUG
        assert "Storing integer setting notification_seconds: 250" in message

    def test_write_raises_returns_false_and_warns(self):
        settings, logs = _make_settings()
        settings._settings.getInt = _Spy(result=100)
        settings._settings.setInt = _Spy(raises=RuntimeError("no dice"))
        assert settings.store_integer_if_changed('notification_seconds', 250) is False
        levels = [level for _, level in logs]
        assert levels == [xbmc.LOGDEBUG, xbmc.LOGWARNING]
        assert "Error storing integer setting 'notification_seconds'" in logs[-1][0]

    def test_pre_read_raising_falls_through_to_write(self):
        settings, logs = _make_settings()
        settings._settings.getInt = _Spy(raises=RuntimeError("read down"))
        set_spy = _Spy()
        settings._settings.setInt = set_spy
        assert settings.store_integer_if_changed('notification_seconds', 250) is True
        assert set_spy.calls == [('notification_seconds', 250)]


# --- intent-level reads ------------------------------------------------------

class TestIntentReads:
    def _spy_bool(self, settings):
        spy = _Spy(result=True)
        settings.get_bool = spy
        return spy

    def _spy_int(self, settings):
        spy = _Spy(result=5)
        settings.get_int = spy
        return spy

    def test_per_fps_offsets_enabled(self):
        settings, _ = _make_settings()
        spy = self._spy_bool(settings)
        assert settings.per_fps_offsets_enabled() is True
        assert spy.calls == [('per_fps_offsets',)]

    def test_pause_enabled(self):
        settings, _ = _make_settings()
        spy = self._spy_bool(settings)
        assert settings.pause_enabled() is True
        assert spy.calls == [('pause_offsets',)]

    def test_remember_adjustments_defaults_on(self):
        settings, _ = _make_settings()
        spy = _Spy(result=False)
        settings.get_bool = spy
        assert settings.remember_adjustments_enabled() is False
        # Learning is the product: the read passes default=True so an
        # unreadable setting NEVER silently disables the learn loop.
        assert spy.calls == [('remember_adjustments', True)]

    def test_classic_gate_reads_are_gone(self):
        settings, _ = _make_settings()
        for dead in ('is_hdr_enabled', 'fps_override_enabled',
                     'active_monitoring_enabled', 'is_new_install'):
            assert not hasattr(settings, dead)

    def test_notifications_enabled(self):
        settings, _ = _make_settings()
        spy = self._spy_bool(settings)
        assert settings.notifications_enabled() is True
        assert spy.calls == [('enable_notifications',)]

    def test_debug_logging_enabled(self):
        settings, _ = _make_settings()
        spy = self._spy_bool(settings)
        assert settings.debug_logging_enabled() is True
        assert spy.calls == [('enable_debug_logging',)]

    def test_seek_back_config_maps_ids(self):
        settings, _ = _make_settings()
        settings.get_bool = _Spy(result=True)
        settings.get_int = _Spy(result=8)
        enabled, seconds = settings.seek_back_config('unpause')
        assert enabled is True
        assert seconds == 8
        assert settings.get_bool.calls == [('enable_seek_back_unpause',)]
        assert settings.get_int.calls == [('seek_back_unpause_seconds',)]

    def test_seek_back_config_clamps_negative_seconds(self):
        settings, _ = _make_settings()
        settings.get_bool = _Spy(result=False)
        settings.get_int = _Spy(result=-4)
        enabled, seconds = settings.seek_back_config('resume')
        assert enabled is False
        assert seconds == 0

    def test_notification_duration_ms_multiplies_by_1000(self):
        settings, _ = _make_settings()
        settings.get_int = _Spy(result=6)
        assert settings.notification_duration_ms() == 6000
        assert settings.get_int.calls == [('notification_seconds',)]


# --- OffsetTable (the store adapter) ------------------------------------------

class _ToggleSettings:
    """Just the per_fps read the adapter consults — flipped between calls to
    prove keys are composed at CALL TIME, never captured."""

    def __init__(self, per_fps=False):
        self.per_fps = per_fps

    def per_fps_offsets_enabled(self):
        return self.per_fps


def _make_table(tmp_path, per_fps=False):
    from resources.lib.aom.store.offset_store import OffsetStore
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
