"""Composition-root tests: graph wiring and the load-bearing dispatch order.

Constructs the REAL ServiceRuntime under Kodistubs. Dispatch follows subscription order per
event type, so the order pins here are behavioral guarantees, not style.
"""

import pytest

from resources.lib.aome.app import events
from resources.lib.aome.app.stream_detector import StreamDetector
from resources.lib.aome.runtime import ServiceRuntime


@pytest.fixture
def runtime():
    return ServiceRuntime()


def test_service_runtime_graph_wiring(runtime):
    # One instance of each adapter, shared by every consumer: the settings
    # doctrine's "single live proxy" and the single-gateway reconciliation.
    assert runtime.detector._gateway is runtime.gateway
    assert runtime.offset_applier._gateway is runtime.gateway
    assert runtime.seek_coordinator._gateway is runtime.gateway
    assert runtime.adjustment_watcher._gateway is runtime.gateway

    assert runtime.detector._settings is runtime.settings
    assert runtime.offset_applier._settings is runtime.settings
    assert runtime.notifier._settings is runtime.settings
    assert runtime.seek_scheduler._settings is runtime.settings
    assert runtime.adjustment_watcher._settings is runtime.settings

    # The sparse store rides behind the OffsetTable seam: one store, loaded
    # at construction, adapter keyed off the live settings toggle.
    assert runtime.offset_applier._offsets is runtime.offsets
    assert runtime.adjustment_watcher._offsets is runtime.offsets
    assert runtime.offsets._store is runtime.store
    assert runtime.offsets._settings is runtime.settings
    assert runtime.notifier._gui is runtime.gui

    for component in (runtime.detector, runtime.offset_applier,
                      runtime.notifier, runtime.seek_scheduler,
                      runtime.adjustment_watcher):
        assert component._sessions is runtime.session_tracker

    # The settle/save split: the seek scheduler's 'change' replay
    # rides the SETTLE (user-action) fact; the notifier's saved toast
    # rides the STORE fact — both typed, both session-stamped.
    settled_handlers = runtime.dispatcher._subscribers[events.UserOffsetSettled]
    assert runtime.seek_scheduler._on_user_offset_settled in settled_handlers
    saved_handlers = runtime.dispatcher._subscribers[events.UserOffsetSaved]
    assert runtime.notifier._on_user_offset_saved in saved_handlers
    assert all(getattr(h, '__self__', None) is not runtime.seek_scheduler
               for h in saved_handlers)


def test_runtime_subscription_order_is_pinned(runtime):
    subs = runtime.dispatcher._subscribers

    def owners(event_type):
        return [getattr(h, '__self__', None) for h in subs[event_type]]

    # Lifecycle AND pause state: the tracker runs FIRST — the session exists
    # (or is torn down) and session.paused is current before any other
    # handler of the same event reads them (session.py states this
    # guarantee for Paused/Resumed explicitly).
    for event_type in (events.PlaybackStarted, events.PlaybackStopped,
                       events.PlaybackEnded, events.Paused, events.Resumed):
        assert owners(event_type)[0] is runtime.session_tracker, (
            f"{event_type.__name__}: SessionTracker must be the first "
            f"subscriber")

    # PlaybackStarted: detector (probe chain) before the seek scheduler
    # (resume request) — detection work is planned before seek work.
    started = owners(events.PlaybackStarted)
    assert started.index(runtime.detector) < started.index(
        runtime.seek_scheduler)

    # ProfileChanged: the applier records session.applied before the watcher
    # evaluates eligibility for the same adoption.
    profile_changed = owners(events.ProfileChanged)
    assert profile_changed.index(runtime.offset_applier) < \
        profile_changed.index(runtime.adjustment_watcher)

    # SettingsChanged: the runtime's debug-flag refresh runs FIRST (the
    # passes for the very save that toggles debug logging must log at the
    # fresh escalation), then the applier's immediate-effect re-apply
    # records session.applied before the watcher's eligibility pass (the
    # same invariant ProfileChanged pins for a stream-change apply).
    settings_changed = owners(events.SettingsChanged)
    assert settings_changed[0] is runtime
    assert settings_changed.index(runtime.offset_applier) < \
        settings_changed.index(runtime.adjustment_watcher)

    # OffsetApplied AND DelayReset: the watcher's structural supersede
    # subscribes to both — every delay the applier sets (apply or silent
    # reset), from any trigger, drops an in-flight observation chain.
    supersede = runtime.adjustment_watcher._on_automatic_delay_set
    assert supersede in subs[events.OffsetApplied]
    assert supersede in subs[events.DelayReset]

    # StoreMutated: the applier reconciles the live session when the
    # management view actually changed the store (immediate deletes).
    assert runtime.offset_applier._on_store_mutated in \
        subs[events.StoreMutated]

    # StreamStabilized: applier (retry pass) -> notifier (pending release)
    # -> seek scheduler (adjust request): offset work strictly precedes
    # seek planning for the same stabilization.
    stabilized = owners(events.StreamStabilized)
    assert (stabilized.index(runtime.offset_applier)
            < stabilized.index(runtime.notifier)
            < stabilized.index(runtime.seek_scheduler))

    # Detection events are the detector's alone.
    assert all(isinstance(owner, StreamDetector)
               for owner in owners(events.ProbeStream))
    assert all(isinstance(owner, StreamDetector)
               for owner in owners(events.VerifyStream))


def test_settings_changed_refreshes_cached_debug_flags(runtime, monkeypatch):
    monkeypatch.setattr(runtime.settings, 'debug_logging_enabled',
                        lambda: True)
    runtime.dispatcher.post(events.SettingsChanged())
    runtime.dispatcher.run_pending()
    assert runtime.logger.debug_escalation is True
    assert runtime.dispatcher.log_runtimes is True

    monkeypatch.setattr(runtime.settings, 'debug_logging_enabled',
                        lambda: False)
    runtime.dispatcher.post(events.SettingsChanged())
    runtime.dispatcher.run_pending()
    assert runtime.logger.debug_escalation is False
    assert runtime.dispatcher.log_runtimes is False


# --- coexistence warning --------------------------------------------------------

class TestCoexistenceWarning:

    def _rig(self, runtime, monkeypatch, *, warned, classic_enabled,
             ok_shown=True, dialog_open=False):
        probes = []
        oks = []
        writes = []
        monkeypatch.setattr(runtime.settings, 'coexistence_warned',
                            lambda: warned)
        monkeypatch.setattr(
            runtime.gateway, 'addon_enabled',
            lambda addon_id: probes.append(addon_id) or classic_enabled)
        monkeypatch.setattr(runtime.gateway, 'settings_dialog_open',
                            lambda: dialog_open)
        monkeypatch.setattr(
            runtime.gui, 'ok',
            lambda heading, message: (oks.append((heading, message))
                                      or ok_shown))
        monkeypatch.setattr(
            runtime.settings, 'store_boolean_if_changed',
            lambda setting_id, value: writes.append((setting_id, value))
            or True)
        return probes, oks, writes

    def test_warns_once_and_sets_flag_when_classic_enabled(self, runtime,
                                                           monkeypatch):
        probes, oks, writes = self._rig(runtime, monkeypatch,
                                        warned=False, classic_enabled=True)

        runtime._maybe_warn_coexistence()

        assert probes == ['script.audiooffsetmanager']
        assert len(oks) == 1
        # kodistubs' getLocalizedString returns '' -> the English fallbacks
        # must fill both halves (a blank warning teaches nothing).
        heading, message = oks[0]
        assert 'Classic Audio Offset Manager' in heading
        assert 'both' in message and 'disabling' in message
        # The once-flag is written ONLY after the dialog actually showed.
        assert writes == [('coexistence_warned', True)]

    def test_flag_set_skips_even_the_probe(self, runtime, monkeypatch):
        probes, oks, writes = self._rig(runtime, monkeypatch,
                                        warned=True, classic_enabled=True)

        runtime._maybe_warn_coexistence()

        assert probes == []
        assert oks == []
        assert writes == []

    def test_classic_absent_warns_nothing_and_keeps_flag_unset(self, runtime,
                                                               monkeypatch):
        # The flag stays unset so classic installed LATER still warns.
        probes, oks, writes = self._rig(runtime, monkeypatch,
                                        warned=False, classic_enabled=False)

        runtime._maybe_warn_coexistence()

        assert probes == ['script.audiooffsetmanager']
        assert oks == []
        assert writes == []

    def test_unrendered_dialog_leaves_flag_unset(self, runtime, monkeypatch):
        # gui.ok answers False when the modal never rendered —
        # the flag means "the user has SEEN this", so it must not be set.
        probes, oks, writes = self._rig(runtime, monkeypatch,
                                        warned=False, classic_enabled=True,
                                        ok_shown=False)

        runtime._maybe_warn_coexistence()

        assert len(oks) == 1                       # the attempt happened
        assert writes == []                        # but nothing persisted

    def test_open_settings_dialog_defers_the_flag_write(self, runtime,
                                                        monkeypatch):
        # A service restart can land under an open
        # settings dialog (addon update/re-enable); writing then would be
        # clobbered by the dialog's save-on-close. Skip — the warning
        # re-fires and writes on a later start.
        probes, oks, writes = self._rig(runtime, monkeypatch,
                                        warned=False, classic_enabled=True,
                                        dialog_open=True)

        runtime._maybe_warn_coexistence()

        assert len(oks) == 1
        assert writes == []
