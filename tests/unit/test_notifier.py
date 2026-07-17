"""Unit tests for aom.app.notifier (Notifier).

Driven exactly like test_adjustment_watcher / test_seek_scheduler: a FakeClock
plus a manually pumped Dispatcher, a real SessionTracker (subscribed FIRST so
the Notifier always sees a live session), a FakeGui recording toasts, a small
local fake settings object, and a real Notifier. Sessions start by posting
``PlaybackStarted``; the profile is set by hand and stream-state is driven with
the session's ``mark_profile_built()`` / ``mark_stable()`` (the detector is not
in the rig).

The dedupe window is derived from ``Notifier.DEDUPE_SECONDS`` so a retune can
never leave these tests green-but-wrong against a stale float.
"""

import pytest

from resources.lib.aom.app import events
from resources.lib.aom.app.dispatcher import Dispatcher
from resources.lib.aom.app.notifier import (
    CORRUPTION_NOTICE_MS, Notifier, STRING_OFFSET_APPLIED,
    STRING_OFFSET_SAVED, STRING_STORE_CORRUPTED)
from resources.lib.aom.app.session import SessionTracker
from resources.lib.aom.domain.profile import StreamProfile
from tests.fakes import FakeClock, FakeGui


DEDUPE = Notifier.DEDUPE_SECONDS
DURATION_MS = 3000
DURATION_S = DURATION_MS / 1000.0
GUARD = Notifier.FADE_GUARD_SECONDS
KODI_MIN_S = Notifier.KODI_MIN_DISPLAY_MS / 1000.0


def make_profile(hdr_type='dolbyvision', audio_format='truehd',
                 video_fps=23.976, player_id=1):
    """A complete profile; summary 'Dolby Vision | 23.976 fps | Dolby TrueHD'."""
    return StreamProfile(hdr_type=hdr_type, audio_format=audio_format,
                         video_fps=video_fps, player_id=player_id,
                         audio_channels=8)


class FakeSettings:
    """The notification read surface: per-kind toast gates and a duration."""

    def __init__(self, duration_ms=DURATION_MS):
        self.apply_enabled = True
        self.learn_enabled = True
        self.duration = duration_ms
        self.per_fps = False

    def notify_apply_enabled(self):
        return self.apply_enabled

    def notify_learn_enabled(self):
        return self.learn_enabled

    def notification_duration_ms(self):
        return self.duration

    def per_fps_offsets_enabled(self):
        return self.per_fps


class Rig:
    """The Notifier assembled on fakes; pump with post/advance."""

    def __init__(self):
        self.clock = FakeClock()
        self.errors = []
        self.debug = []
        self.dispatcher = Dispatcher(clock=self.clock,
                                     log_error=self.errors.append,
                                     log_debug=self.debug.append)
        # Tracker subscribes lifecycle FIRST (rig convention / dispatch order).
        self.tracker = SessionTracker(self.dispatcher, clock=self.clock,
                                      log_debug=self.debug.append)
        self.gui = FakeGui()
        self.settings = FakeSettings()
        self.notifier = Notifier(self.dispatcher, self.tracker, self.settings,
                                 self.gui, clock=self.clock,
                                 log_debug=self.debug.append)

    # -- pumping ----------------------------------------------------------------

    def post(self, event):
        self.dispatcher.post(event)
        self.dispatcher.run_pending()

    def advance(self, seconds):
        self.clock.advance(seconds)
        self.dispatcher.run_pending()

    # -- convenience ------------------------------------------------------------

    @property
    def session(self):
        return self.tracker.current

    @property
    def toasts(self):
        return self.gui.notifications

    def start(self, profile=None):
        """Begin a session and hand it a profile; return the session."""
        self.post(events.PlaybackStarted())
        session = self.session
        session.profile = profile
        return session

    def mark_stable(self, session):
        """STARTING -> STABILIZING -> STABLE (detector-order parity)."""
        session.mark_profile_built()
        session.mark_stable()

    def applied(self, session, profile, ms):
        """Post a non-provisional OffsetApplied for the session."""
        self.post(events.OffsetApplied(session_id=session.session_id,
                                       profile=profile, ms=ms,
                                       provisional=False))

    def logged(self, needle):
        return any(needle in line for line in self.debug)


@pytest.fixture
def rig():
    return Rig()


def test_manual_save_supersedes_a_held_provisional_toast(rig):
    # A user adjustment landing INSIDE the provisional window makes the held
    # ms stale: releasing it on stabilization would announce a value that no
    # longer applies (legacy cleared the pending toast on its non-suppressed
    # apply path before the equivalent sequence could surface it). The manual
    # save clears the hold; stabilization then releases nothing.
    profile = make_profile()
    session = rig.start(profile)

    rig.post(events.OffsetApplied(session_id=session.session_id,
                                  profile=profile, ms=30, provisional=True))
    assert session.pending_notification is not None
    assert rig.toasts == []

    rig.post(events.UserOffsetSaved(session_id=session.session_id,
                                    profile=profile, ms=50))
    assert session.pending_notification is None       # hold superseded
    # per_fps is OFF in the rig: the rate is omitted from the summary and
    # the saved line rides as the toast TITLE (E7 beta1 field fix).
    assert rig.toasts == [("Dolby Vision | Dolby TrueHD", DURATION_MS)]
    assert rig.gui.titles == ["#32093: +50 ms"]

    rig.mark_stable(session)
    rig.post(events.StreamStabilized(session_id=session.session_id))
    assert rig.toasts == [("Dolby Vision | Dolby TrueHD", DURATION_MS)]
    # No stale "applied +30" ever surfaces.


# ============================================================================
# Immediate (non-provisional) application
# ============================================================================

class TestImmediateApply:

    def test_non_provisional_toasts_immediately_with_legacy_message(self, rig):
        profile = make_profile()
        session = rig.start(profile)

        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=profile, ms=-125,
                                      provisional=False))

        assert rig.toasts == [("Dolby Vision | Dolby TrueHD", DURATION_MS)]
        assert rig.gui.titles == ["#32092: -125 ms"]
        assert session.pending_notification is None

    def test_non_provisional_clears_any_prior_pending(self, rig):
        profile = make_profile()
        session = rig.start(profile)
        session.pending_notification = (profile, -999)

        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=profile, ms=-50,
                                      provisional=False))
        assert session.pending_notification is None
        assert rig.toasts == [("Dolby Vision | Dolby TrueHD", DURATION_MS)]
        assert rig.gui.titles == ["#32092: -50 ms"]


# ============================================================================
# Deferral until STABLE
# ============================================================================

class TestDeferral:

    def test_provisional_holds_then_stabilization_releases_once(self, rig):
        profile = make_profile()
        session = rig.start(profile)

        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=profile, ms=-75,
                                      provisional=True))
        assert session.pending_notification == (profile, -75)
        assert rig.toasts == []

        # Detector marks STABLE, THEN posts StreamStabilized (queue order).
        rig.mark_stable(session)
        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert rig.toasts == [("Dolby Vision | Dolby TrueHD", DURATION_MS)]
        assert rig.gui.titles == ["#32092: -75 ms"]
        assert session.pending_notification is None
        assert rig.logged("Released pending offset notification")

    def test_pending_dropped_when_profile_changed_underneath(self, rig):
        profile_a = make_profile(hdr_type='dolbyvision', audio_format='truehd')
        profile_b = make_profile(hdr_type='hdr10', audio_format='eac3')
        assert profile_a.identity() != profile_b.identity()
        session = rig.start(profile_a)

        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=profile_a, ms=-75,
                                      provisional=True))
        # Profile swapped before stabilization: the held key is now stale.
        session.profile = profile_b
        rig.mark_stable(session)
        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert rig.toasts == []
        assert session.pending_notification is None

    def test_stabilization_with_no_pending_does_nothing(self, rig):
        profile = make_profile()
        session = rig.start(profile)
        rig.mark_stable(session)

        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert rig.toasts == []
        assert session.pending_notification is None

    def test_stabilization_before_stable_state_does_not_release(self, rig):
        # Defensive parity: a StreamStabilized while the session is not yet
        # STABLE must not release the pending toast.
        profile = make_profile()
        session = rig.start(profile)
        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=profile, ms=-75,
                                      provisional=True))
        # NOT marked stable -> stream_state is STARTING.
        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert rig.toasts == []
        assert session.pending_notification == (profile, -75)

    def test_newest_pending_wins_across_provisional_applies(self, rig):
        # A provisional apply under A, then a profile change and a NEW
        # provisional apply under B: only B's toast releases.
        profile_a = make_profile(hdr_type='dolbyvision', audio_format='truehd')
        profile_b = make_profile(hdr_type='hdr10', audio_format='eac3')
        session = rig.start(profile_a)

        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=profile_a, ms=-75,
                                      provisional=True))
        assert session.pending_notification == (profile_a, -75)

        session.profile = profile_b
        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=profile_b, ms=-40,
                                      provisional=True))
        assert session.pending_notification == (profile_b, -40)

        rig.mark_stable(session)
        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert rig.toasts == [("HDR10 | Dolby Digital Plus", DURATION_MS)]
        assert rig.gui.titles == ["#32092: -40 ms"]
        assert session.pending_notification is None


# ============================================================================
# Manual offset saved
# ============================================================================

class TestUserOffsetSaved:

    def test_saved_toasts_from_event_profile_not_session(self, rig):
        # The toast describes the EVENT's profile/ms, even when session.profile
        # has moved on (the watcher captured it at store time).
        event_profile = make_profile(hdr_type='dolbyvision', audio_format='truehd')
        session_profile = make_profile(hdr_type='hlg', audio_format='ac3')
        session = rig.start(session_profile)

        rig.post(events.UserOffsetSaved(session_id=session.session_id,
                                        profile=event_profile, ms=60))

        assert rig.toasts == [("Dolby Vision | Dolby TrueHD", DURATION_MS)]
        assert rig.gui.titles == ["#32093: +60 ms"]


# ============================================================================
# Stale / superseded session stamps are inert
# ============================================================================

class TestStaleSessions:

    def test_offset_applied_for_dead_session_is_inert(self, rig):
        profile = make_profile()
        session = rig.start(profile)
        dead_id = session.session_id
        rig.post(events.PlaybackStopped())          # session gone
        assert rig.session is None

        rig.post(events.OffsetApplied(session_id=dead_id, profile=profile,
                                      ms=-50, provisional=False))
        assert rig.toasts == []

    def test_user_offset_saved_for_superseded_session_is_inert(self, rig):
        profile = make_profile()
        session = rig.start(profile)
        old_id = session.session_id
        rig.post(events.PlaybackStarted())          # in-place reopen -> new id
        assert rig.session.session_id != old_id

        rig.post(events.UserOffsetSaved(session_id=old_id, profile=profile,
                                        ms=-50))
        assert rig.toasts == []

    def test_stream_stabilized_for_dead_session_is_inert(self, rig):
        profile = make_profile()
        session = rig.start(profile)
        dead_id = session.session_id
        session.pending_notification = (profile, -50)
        rig.post(events.PlaybackStopped())

        rig.post(events.StreamStabilized(session_id=dead_id))
        assert rig.toasts == []


# ============================================================================
# Dedupe window
# ============================================================================

class TestDedupe:

    def test_identical_within_window_suppressed_after_window_toasts(self, rig):
        profile = make_profile()
        session = rig.start(profile)

        rig.applied(session, profile, -50)
        rig.applied(session, profile, -50)                 # same key, same time
        assert len(rig.toasts) == 1

        rig.advance(DEDUPE + 0.5)                           # past the window
        rig.applied(session, profile, -50)
        assert len(rig.toasts) == 2

    def test_different_ms_inside_window_not_suppressed(self, rig):
        profile = make_profile()
        session = rig.start(profile)

        rig.applied(session, profile, -50)
        rig.applied(session, profile, -75)                 # different ms -> key differs
        assert len(rig.toasts) == 2

    def test_applied_does_not_suppress_saved(self, rig):
        # Same profile identity and ms but different string_id -> distinct dedupe key.
        profile = make_profile()
        session = rig.start(profile)

        rig.applied(session, profile, -50)
        rig.post(events.UserOffsetSaved(session_id=session.session_id,
                                        profile=profile, ms=-50))
        assert len(rig.toasts) == 2
        assert rig.gui.titles[0].startswith(f"#{STRING_OFFSET_APPLIED}")
        assert rig.gui.titles[1].startswith(f"#{STRING_OFFSET_SAVED}")


# ============================================================================
# Fade guard (Kodi close-animation swallow)
# ============================================================================

class TestFadeGuard:
    """The guard defers exactly the toasts that would ride the previous
    toast's fade-out, and no others (Kodi mechanics: notifier module doc)."""

    def test_toast_inside_fade_window_is_deferred_past_it(self, rig):
        # The field repro (2.0.0~beta2, CoreELEC): an "applied" toast landing
        # ~200ms after the previous toast's display time expired rode its
        # fade-out and flashed for ~100ms. It must defer past the fade and
        # then show for its full duration.
        profile = make_profile()
        session = rig.start(profile)
        rig.applied(session, profile, -50)
        assert len(rig.toasts) == 1

        rig.advance(DURATION_S + 0.2)           # inside [shown, shown+guard)
        rig.applied(session, profile, -75)
        assert len(rig.toasts) == 1             # deferred, not swallowed
        assert rig.logged("deferring toast")

        rig.advance(GUARD - 0.2)                # guarded window has passed
        # per_fps is OFF in the rig: rate omitted, heading rides as TITLE.
        assert rig.toasts[1] == ("Dolby Vision | Dolby TrueHD", DURATION_MS)
        assert rig.gui.titles[1] == "#32092: -75 ms"

    def test_toast_while_window_still_open_fires_immediately(self, rig):
        # Mid-display Kodi swaps the content in place and restarts the
        # display timer — native behavior, no deferral allowed.
        profile = make_profile()
        session = rig.start(profile)
        rig.applied(session, profile, -50)

        rig.advance(DURATION_S - 0.5)
        rig.applied(session, profile, -75)
        assert len(rig.toasts) == 2

    def test_toast_after_fade_window_fires_immediately(self, rig):
        profile = make_profile()
        session = rig.start(profile)
        rig.applied(session, profile, -50)

        rig.advance(DURATION_S + GUARD)         # window fully closed
        rig.applied(session, profile, -75)
        assert len(rig.toasts) == 2

    def test_newest_contender_supersedes_a_deferred_toast(self, rig):
        # Two toasts contending for one fade window: the release is
        # key-replaced, so only the newest (freshest fact) surfaces.
        profile = make_profile()
        session = rig.start(profile)
        rig.applied(session, profile, -50)

        rig.advance(DURATION_S + 0.1)
        rig.applied(session, profile, -75)          # deferred
        rig.advance(0.1)
        rig.applied(session, profile, -60)          # key-replaces the -75

        rig.advance(GUARD)
        assert len(rig.toasts) == 2
        assert rig.gui.titles[1] == "#32092: -60 ms"

    def test_immediate_raise_cancels_a_pending_deferred_toast(self, rig):
        # The boundary race (review finding, confirmed against the real
        # dispatcher): an event dequeued just before the deferred release
        # deadline whose handler's clock read lands past it raises
        # immediately — the pending deferred toast is stale at that instant
        # and must be cancelled, or it fires right after and paints its
        # older value over the fresher one.
        profile = make_profile()
        session = rig.start(profile)
        rig.applied(session, profile, -50)

        rig.advance(DURATION_S + 0.2)
        rig.applied(session, profile, -75)          # deferred, due at +GUARD
        assert len(rig.toasts) == 1

        # Sit just before the deadline, then let the enabled-gate read
        # consume time so the guard's clock read crosses it — the
        # handler-latency race the dispatcher's ordering cannot prevent.
        rig.advance(GUARD - 0.25)
        original = rig.settings.notify_apply_enabled
        def slow_enabled():
            rig.clock.advance(0.1)
            return original()
        rig.settings.notify_apply_enabled = slow_enabled
        rig.applied(session, profile, -60)          # immediate (past band)
        del rig.settings.notify_apply_enabled

        assert rig.gui.titles[-1] == "#32092: -60 ms"
        rig.advance(GUARD + 1.0)                    # stale timer must be dead
        assert len(rig.toasts) == 2
        assert not any("-75" in title for title in rig.gui.titles)

    def test_deferred_toast_suppressed_when_notifications_disabled(self, rig):
        # The enabled gate is a live setting: a toast deferred while
        # notifications were on must not fire after the user turns them off.
        profile = make_profile()
        session = rig.start(profile)
        rig.applied(session, profile, -50)

        rig.advance(DURATION_S + 0.2)
        rig.applied(session, profile, -75)          # deferred
        rig.settings.apply_enabled = False

        rig.advance(GUARD)
        assert len(rig.toasts) == 1

    def test_deferred_toast_rechecks_its_own_gate_not_the_other(self, rig):
        # The gate rides on the RaiseToast event (D10: per-kind toggles), so
        # a deferred APPLY toast re-checks notify_apply at fire time and is
        # untouched by the learn toggle flipping off inside the deferral.
        profile = make_profile()
        session = rig.start(profile)
        rig.applied(session, profile, -50)

        rig.advance(DURATION_S + 0.2)
        rig.applied(session, profile, -75)          # deferred
        rig.settings.learn_enabled = False

        rig.advance(GUARD)
        assert len(rig.toasts) == 2

    def test_guard_uses_kodis_display_floor_not_the_raw_setting(self, rig):
        # Kodi clamps displayTime to KODI_MIN_DISPLAY_MS: with a 1s user
        # duration the window is really open for 1.5s, so a toast at 1.2s is
        # still an in-place swap (immediate) and 1.6s is inside the CLAMPED
        # display's fade window (deferred).
        rig.settings.duration = 1000
        profile = make_profile()
        session = rig.start(profile)
        rig.applied(session, profile, -50)

        rig.advance(1.2)                        # raw duration passed; clamp not
        rig.applied(session, profile, -75)
        assert len(rig.toasts) == 2             # window still open: swap

        rig.advance(KODI_MIN_S + 0.1)           # 1.6s after the second raise
        rig.applied(session, profile, -60)
        assert len(rig.toasts) == 2             # deferred

        rig.advance(GUARD)
        assert len(rig.toasts) == 3

    def test_deferred_toast_survives_session_end(self, rig):
        # RaiseToast is deliberately not session-stamped: the payload
        # announces a store/apply that already happened and stays true even
        # if playback stops inside the deferral.
        profile = make_profile()
        session = rig.start(profile)
        rig.applied(session, profile, -50)

        rig.advance(DURATION_S + 0.2)
        rig.applied(session, profile, -75)          # deferred
        rig.post(events.PlaybackStopped())

        rig.advance(GUARD)
        assert len(rig.toasts) == 2


# ============================================================================
# Settings gate and message sign rendering
# ============================================================================

class TestSettingsGate:

    def test_disabled_suppresses_toast_but_pending_logic_still_runs(self, rig):
        rig.settings.apply_enabled = False
        profile = make_profile()
        session = rig.start(profile)

        # Provisional hold still updates pending, without toasting.
        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=profile, ms=-75,
                                      provisional=True))
        assert session.pending_notification == (profile, -75)
        assert rig.toasts == []

        # Release still clears pending, still no toast.
        rig.mark_stable(session)
        rig.post(events.StreamStabilized(session_id=session.session_id))
        assert session.pending_notification is None
        assert rig.toasts == []

    def test_apply_gate_off_still_toasts_learn(self, rig):
        # D10: the gates are independent — muting the routine apply toasts
        # must never silence the learn feedback (the teaching surface).
        rig.settings.apply_enabled = False
        profile = make_profile()
        session = rig.start(profile)

        rig.post(events.UserOffsetSaved(session_id=session.session_id,
                                        profile=profile, ms=-115))

        assert len(rig.toasts) == 1
        assert rig.gui.titles[0].startswith(f"#{STRING_OFFSET_SAVED}")

    def test_learn_gate_off_still_toasts_apply(self, rig):
        rig.settings.learn_enabled = False
        profile = make_profile()
        session = rig.start(profile)

        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=profile, ms=-75,
                                      provisional=False))

        assert len(rig.toasts) == 1
        assert rig.gui.titles[0].startswith(f"#{STRING_OFFSET_APPLIED}")

    def test_learn_gate_off_suppresses_saved_toast(self, rig):
        rig.settings.learn_enabled = False
        profile = make_profile()
        session = rig.start(profile)

        rig.post(events.UserOffsetSaved(session_id=session.session_id,
                                        profile=profile, ms=-115))

        assert rig.toasts == []


class TestUnsavedOffsetDiscarded:
    # D3 amendment (E7): the zero-reset discarded a manual adjustment
    # that never reached the store — save-related feedback, so it lives
    # under the LEARN gate.

    def _discard(self, rig, session):
        rig.post(events.UnsavedOffsetDiscarded(
            session_id=session.session_id, profile=session.profile, ms=-50))

    def test_toast_shape(self, rig):
        session = rig.start(make_profile())
        self._discard(rig, session)

        assert rig.toasts == [("#32133", DURATION_MS)]
        assert rig.gui.titles == ["#32132"]

    def test_gated_by_notify_learn_not_apply(self, rig):
        session = rig.start(make_profile())

        rig.settings.apply_enabled = False           # irrelevant gate
        self._discard(rig, session)
        assert len(rig.toasts) == 1

        rig.settings.learn_enabled = False           # the owning gate
        self._discard(rig, session)
        assert len(rig.toasts) == 1                  # suppressed

    def test_dead_session_is_inert(self, rig):
        session = rig.start(make_profile())
        rig.post(events.PlaybackStopped())

        rig.post(events.UnsavedOffsetDiscarded(
            session_id=session.session_id, profile=session.profile, ms=-50))

        assert rig.toasts == []

    def test_blank_localization_falls_back_to_english(self, rig):
        session = rig.start(make_profile())
        rig.gui.localized_strings[32132] = ''
        rig.gui.localized_strings[32133] = ''

        self._discard(rig, session)

        message, _duration = rig.toasts[0]
        assert 'nothing stored' in message
        assert rig.gui.titles == ["Offset not saved"]


class TestStoreCorrupted:
    # The corruption notice is an error notice, not a per-kind toast: it
    # has no session, ignores the notify gates, and uses its own duration.

    def test_notice_fires_without_a_session(self, rig):
        assert rig.session is None
        rig.post(events.StoreCorrupted())
        assert rig.toasts == [(f"#{STRING_STORE_CORRUPTED}",
                               CORRUPTION_NOTICE_MS)]

    def test_notice_ignores_both_toast_gates(self, rig):
        rig.settings.apply_enabled = False
        rig.settings.learn_enabled = False
        rig.post(events.StoreCorrupted())
        assert len(rig.toasts) == 1

    def test_blank_localization_falls_back_to_english(self, rig):
        # localized() degrades to '' on failure; the notice is the user's
        # ONLY signal their offsets were reset and must never be blank.
        rig.gui.localized_strings[STRING_STORE_CORRUPTED] = ''
        rig.post(events.StoreCorrupted())
        message, duration = rig.toasts[0]
        assert 'offsets.json.bad' in message
        assert duration == CORRUPTION_NOTICE_MS


class TestSignRendering:

    @pytest.mark.parametrize("ms, rendered", [
        (75, "+75 ms"),
        (-75, "-75 ms"),
        (0, "0 ms"),
    ])
    def test_sign_rendering(self, rig, ms, rendered):
        profile = make_profile()
        session = rig.start(profile)

        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=profile, ms=ms, provisional=False))

        assert rig.toasts == [("Dolby Vision | Dolby TrueHD", DURATION_MS)]
        assert rig.gui.titles == [f"#32092: {rendered}"]


class TestToastShape:
    # E7 beta1 field fix (Kodi 22 beta1 / Windows): the saved/applied line
    # rides as the toast TITLE and the message is ONLY the profile summary
    # — a newline-packed single message made Kodi's label auto-scroll
    # (perceived as flashing) and truncated the codec off the end.

    def test_rate_is_omitted_when_per_fps_is_off(self, rig):
        # With the toggle off the value lives under the all-rates key:
        # showing "23.976 fps" both misleads and crowds out the codec.
        profile = make_profile()
        session = rig.start(profile)

        rig.post(events.UserOffsetSaved(session_id=session.session_id,
                                        profile=profile, ms=-100))

        message, _duration = rig.toasts[0]
        assert message == "Dolby Vision | Dolby TrueHD"
        assert "fps" not in message
        assert "\n" not in message

    def test_rate_is_shown_when_per_fps_is_on(self, rig):
        rig.settings.per_fps = True
        profile = make_profile()
        session = rig.start(profile)

        rig.post(events.UserOffsetSaved(session_id=session.session_id,
                                        profile=profile, ms=-100))

        assert rig.toasts == [("Dolby Vision | 23.976 fps | Dolby TrueHD",
                               DURATION_MS)]
        assert rig.gui.titles == ["#32093: -100 ms"]


class TestIdentityGranularity:

    def test_held_toast_survives_fps_wiggle_when_per_fps_off(self, rig):
        # E2 review finding: with per_fps OFF the detector treats an fps
        # re-read as the SAME stream (silent profile refresh) — the held
        # toast must use the same identity notion and still release.
        session = rig.start(make_profile(video_fps=23.976))
        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=session.profile, ms=-125,
                                      provisional=True))
        assert session.pending_notification is not None

        # The detector's silent incidental-field refresh: fps wiggles across
        # the integer boundary, offset-relevant identity unchanged.
        session.profile = make_profile(video_fps=24.0)
        rig.mark_stable(session)
        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert len(rig.toasts) == 1                    # toast NOT dropped

    def test_held_toast_drops_on_fps_change_when_per_fps_on(self, rig):
        # With per_fps ON the rate IS offset-relevant: a changed rate means
        # the held toast describes a stale stream and must drop.
        rig.settings.per_fps = True
        session = rig.start(make_profile(video_fps=23.976))
        rig.post(events.OffsetApplied(session_id=session.session_id,
                                      profile=session.profile, ms=-125,
                                      provisional=True))

        session.profile = make_profile(video_fps=24.0)
        rig.mark_stable(session)
        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert rig.toasts == []                        # stale toast dropped
        assert session.pending_notification is None

