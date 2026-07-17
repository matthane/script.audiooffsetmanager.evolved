"""Behavioral tests for aom.kodi.settings (the Settings adapter).

Kodi is faked via Kodistubs: ``xbmcaddon.Addon(...).getSettings()`` yields a
stub ``Settings`` object whose ``getBool``/``getInt``/``setBool``/``setInt`` we
spy or monkeypatch per test. Log-sink calls are collected in a list of
``(message, level)`` tuples so assertions can pin both the message text and the
Kodi log level. No fixtures beyond ``monkeypatch``.
"""

import xbmc

from resources.lib.aom.kodi.settings import ADDON_ID, Settings
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
    assert ADDON_ID == 'script.audiooffsetmanager.evolved'


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


class TestGetStringList:
    def test_returns_a_plain_list(self):
        settings, logs = _make_settings()
        settings._settings.getStringList = _Spy(result=('unpause', 'change'))
        assert settings.get_string_list('seek_back_events') == [
            'unpause', 'change']
        assert logs == []

    def test_error_returns_empty_list_and_warns(self):
        # Empty list = "no options selected" = every list setting's
        # do-nothing state (fail-quiet doctrine, like the other reads).
        settings, logs = _make_settings()
        settings._settings.getStringList = _Spy(raises=RuntimeError("boom"))
        assert settings.get_string_list('seek_back_events') == []
        message, level = logs[0]
        assert level == xbmc.LOGWARNING
        assert "Error getting string list setting 'seek_back_events'" in message


# --- store_*_if_changed ------------------------------------------------------

class TestStoreBooleanIfChanged:
    def test_unchanged_skips_write_returns_true(self):
        settings, logs = _make_settings()
        settings._settings.getBool = _Spy(result=True)
        set_spy = _Spy()
        settings._settings.setBool = set_spy
        assert settings.store_boolean_if_changed('apply_offsets', True) is True
        assert set_spy.calls == []
        assert logs == []

    def test_changed_writes_returns_true_and_logs_debug(self):
        settings, logs = _make_settings()
        settings._settings.getBool = _Spy(result=False)
        set_spy = _Spy()
        settings._settings.setBool = set_spy
        assert settings.store_boolean_if_changed('apply_offsets', True) is True
        assert set_spy.calls == [('apply_offsets', True)]
        assert len(logs) == 1
        message, level = logs[0]
        assert level == xbmc.LOGDEBUG
        assert "Storing boolean setting apply_offsets: True" in message

    def test_write_raises_returns_false_and_warns(self):
        settings, logs = _make_settings()
        settings._settings.getBool = _Spy(result=False)
        settings._settings.setBool = _Spy(raises=RuntimeError("no dice"))
        assert settings.store_boolean_if_changed('apply_offsets', True) is False
        # LOGDEBUG "Storing" then LOGWARNING "Error storing".
        levels = [level for _, level in logs]
        assert levels == [xbmc.LOGDEBUG, xbmc.LOGWARNING]
        assert "Error storing boolean setting 'apply_offsets'" in logs[-1][0]

    def test_pre_read_raising_falls_through_to_write(self):
        settings, logs = _make_settings()
        # get_bool swallows the read error -> default False; value True differs,
        # so the store falls through to the write attempt (like legacy).
        settings._settings.getBool = _Spy(raises=RuntimeError("read down"))
        set_spy = _Spy()
        settings._settings.setBool = set_spy
        assert settings.store_boolean_if_changed('apply_offsets', True) is True
        assert set_spy.calls == [('apply_offsets', True)]


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

    def test_apply_enabled_defaults_on(self):
        settings, _ = _make_settings()
        spy = _Spy(result=False)
        settings.get_bool = spy
        assert settings.apply_enabled() is False
        # Applying is the product's other half: the read passes
        # default=True so an unreadable setting never silently disables it
        # (same doctrine as the learn toggle).
        assert spy.calls == [('apply_offsets', True)]

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

    def test_notify_apply_defaults_on(self):
        settings, _ = _make_settings()
        spy = _Spy(result=False)
        settings.get_bool = spy
        assert settings.notify_apply_enabled() is False
        # D10: the toasts are the teaching surface — the read passes
        # default=True so an unreadable setting never silently mutes them.
        assert spy.calls == [('notify_apply', True)]

    def test_notify_learn_defaults_on(self):
        settings, _ = _make_settings()
        spy = _Spy(result=False)
        settings.get_bool = spy
        assert settings.notify_learn_enabled() is False
        assert spy.calls == [('notify_learn', True)]

    def test_single_notifications_gate_is_gone(self):
        # D10 split the classic all-or-nothing gate into per-kind toggles.
        settings, _ = _make_settings()
        assert not hasattr(settings, 'notifications_enabled')

    def test_debug_logging_enabled(self):
        settings, _ = _make_settings()
        spy = self._spy_bool(settings)
        assert settings.debug_logging_enabled() is True
        assert spy.calls == [('enable_debug_logging',)]

    def test_coexistence_warned(self):
        settings, _ = _make_settings()
        spy = self._spy_bool(settings)
        assert settings.coexistence_warned() is True
        # Default False: an unreadable flag means "warn again" — the
        # warning is idempotent-annoying at worst, silence is worse.
        assert spy.calls == [('coexistence_warned',)]

    def test_seek_back_config_is_list_membership_plus_shared_seconds(self):
        # The four enable toggles collapsed into one multiselect whose
        # option values are the scheduler's reason vocabulary verbatim,
        # and the four sliders into one shared amount.
        settings, _ = _make_settings()
        settings.get_string_list = _Spy(result=['unpause', 'change'])
        settings.get_int = _Spy(result=8)
        enabled, seconds = settings.seek_back_config('unpause')
        assert enabled is True
        assert seconds == 8
        assert settings.get_string_list.calls == [('seek_back_events',)]
        assert settings.get_int.calls == [('seek_back_seconds',)]

        enabled, _seconds = settings.seek_back_config('resume')
        assert enabled is False               # not a member -> disabled

    def test_seek_back_config_clamps_negative_seconds(self):
        settings, _ = _make_settings()
        settings.get_string_list = _Spy(result=[])
        settings.get_int = _Spy(result=-4)
        enabled, seconds = settings.seek_back_config('resume')
        assert enabled is False
        assert seconds == 0

    def test_notification_duration_ms_multiplies_by_1000(self):
        settings, _ = _make_settings()
        settings.get_int = _Spy(result=6)
        assert settings.notification_duration_ms() == 6000
        # default=5 so an unreadable setting yields a visible 5 s toast,
        # never a 0 ms blink.
        assert settings.get_int.calls == [('notification_seconds', 5)]
