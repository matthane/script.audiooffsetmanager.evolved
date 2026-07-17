"""User-facing offset notifications — the app-layer toast owner.

Replaces the legacy NotificationHandler AND the pending-notification dance
OffsetManager used to run inline (``_maybe_send_pending_notification`` plus the
provisional-suppression block in ``apply_audio_offset``). Everything about
"should a toast fire, and which message" now lives here, driven by typed
events on the dispatcher thread:

* ``OffsetApplied`` — an automatic apply. A provisional apply (the stream is
  not yet STABLE) does NOT toast; its message is HELD on
  ``session.pending_notification`` and released on the session's next
  ``StreamStabilized``. A non-provisional apply toasts immediately and clears
  any pending hold.
* ``StreamStabilized`` — releases a held provisional toast, but only if the
  profile still has the identity it was held under: a profile that changed
  underneath drops the stale toast (freshness doctrine — never announce a
  stale stream). Identity uses ``policies.stream_identity`` with the LIVE
  ``per_fps_offsets`` toggle — the SAME notion the detector's same-stream
  refresh uses — so an fps wiggle the offset system deliberately ignores
  (toggle off) can never drop a toast for an apply that really happened
  (E2 review finding: ``profile.identity()`` here disagreed with the
  detector exactly when the toggle was off). The held profile rides on the
  hold whole, and both sides of the comparison are derived fresh at
  release time.
* ``UserOffsetSaved`` — a manual adjustment the AdjustmentWatcher stored.
  Toasts from the event's own profile/ms (captured at store time on the
  dispatcher thread); session/settings are deliberately NOT re-read.

Deferral-until-stable and the 1s duplicate-suppression window are both ported
from the legacy path. The dedupe clock is the injected ``time.monotonic`` — a
deliberate upgrade from the legacy ``time.time``, which mis-measured the
window across wall-clock adjustments.

The fade guard (classic 2.0.0~beta3 field fix, cherry-picked) covers a Kodi
GUI hazard the legacy path never noticed: GUIDialogKaiToast swaps a queued
toast's content into the window in place while it is showing (restarting the
display timer, window stays open — fine) and opens fresh when fully closed
(fine), but a toast popping from Kodi's queue during the window's CLOSE
ANIMATION is painted onto the dying window and vanishes with the fade. A
toast raised in roughly [duration, duration + fade] after its predecessor is
therefore swallowed (observed in the wild: an "applied" toast landing 5.2s
after a 5s "saved" toast flashed for ~100ms). EVERY toast the notifier
raises — the offset toasts AND the discard/corruption notices — flows
through one choke point (``_present``/``_raise``) that remembers when the
last toast was raised and for how long, and ONLY a toast that would land
inside that guarded window is deferred — released past the fade via a
scheduled ``RaiseToast`` carrying the surface pre-rendered at request time
(key-replaced: the newest contender wins, across message kinds too — the
survivor is always the fresher fact — and an immediate raise cancels any
pending release outright). Every other toast fires immediately, exactly as
before. Best-effort by design: toasts raised by Kodi itself or OTHER addons
share the same GUI window but are invisible to this bookkeeping.

Settings are read through the injected facade: the per-kind toast gates
``notify_apply_enabled`` / ``notify_learn_enabled`` (D10: each toast kind has
its own toggle, both default ON) plus ``notification_duration_ms``. Toasts go
through the injected gui. Pure app layer: stdlib + ``resources.lib.aom`` only.
"""

import time

from resources.lib.aom.app import events
from resources.lib.aom.domain import policies
from resources.lib.aom.domain.stream_state import StreamState
from resources.lib.aom.store import keys as store_keys

STRING_OFFSET_APPLIED = 32092
STRING_OFFSET_SAVED = 32093
# "Stored offsets were unreadable and were reset (backup kept as
# offsets.json.bad)" — the startup corruption notice (moved here from the
# runtime with the typed StoreCorrupted event, per the E4 ledger).
STRING_STORE_CORRUPTED = 32121
CORRUPTION_NOTICE_MS = 7000
# "Offset not saved" / "Reset to 0 ms. Nothing is stored for this stream":
# the zero-reset discarded a manual adjustment that never reached the
# store (D3 amendment, E7).
STRING_OFFSET_NOT_SAVED = 32132
STRING_RESET_BASELINE = 32133


class Notifier:
    """Owns offset toasts: deferral-until-stable, dedupe, and the fade guard."""

    DEDUPE_SECONDS = 1.0
    # Width of the guarded window after a toast's display time expires — and
    # therefore where the deferred release lands. Budget: Kodi's display timer
    # starts at the END of the window's open animation (so true expiry lags
    # our raise stamp by up to a few hundred ms), the skin-defined close
    # animation runs a few hundred more, and the release must land PAST that
    # total with margin, never at its edge. One constant governs both the
    # detection band and the release target so no unguarded slice can open
    # between them.
    FADE_GUARD_SECONDS = 1.25
    # GUIDialogKaiToast::AddToQueue clamps displayTime to a floor of
    # TOAST_MESSAGE_TIME (1000) + 500, whatever the caller asked for.
    KODI_MIN_DISPLAY_MS = 1500

    _FADE_KEY = 'aom.notifier.toast'

    def __init__(self, dispatcher, session_tracker, settings, gui,
                 clock=time.monotonic, *, log_debug):
        self._dispatcher = dispatcher
        self._sessions = session_tracker
        self._settings = settings
        self._gui = gui
        self._clock = clock
        self._log = log_debug
        # The last raised toast, or None: (dedupe key, monotonic stamp,
        # duration given). One field so the dedupe/fade-guard lockstep is
        # structural rather than by convention.
        self._last_raise = None

        dispatcher.subscribe(events.OffsetApplied, self._on_offset_applied)
        dispatcher.subscribe(events.UserOffsetSaved, self._on_user_offset_saved)
        dispatcher.subscribe(events.StreamStabilized, self._on_stream_stabilized)
        dispatcher.subscribe(events.StoreCorrupted, self._on_store_corrupted)
        dispatcher.subscribe(events.UnsavedOffsetDiscarded,
                             self._on_unsaved_discarded)
        dispatcher.subscribe(events.RaiseToast, self._on_raise_toast)

    # -- handlers (dispatcher thread) -------------------------------------------

    def _on_offset_applied(self, event):
        if not self._sessions.is_alive(event.session_id):
            return
        session = self._sessions.current
        if event.provisional:
            # Held until the stream stabilizes; the WHOLE profile rides on
            # the hold so the release can compare identities at the
            # granularity in force THEN (toggle read at release instant).
            session.pending_notification = (event.profile, event.ms)
            self._log("AOMe_Notifier: holding provisional notification until "
                      "the stream stabilizes")
            return
        session.pending_notification = None
        self._toast(STRING_OFFSET_APPLIED, event.ms, event.profile,
                    enabled=self._settings.notify_apply_enabled)

    def _on_stream_stabilized(self, event):
        if not self._sessions.is_alive(event.session_id):
            return
        session = self._sessions.current
        if session.pending_notification is None:
            return
        # Defensive parity with the legacy release check: only release once the
        # session is genuinely STABLE.
        if session.stream_state is not StreamState.STABLE:
            return
        pending_profile, pending_ms = session.pending_notification
        # Read the profile FRESH, and compare at the granularity the
        # OFFSET system uses right now (the detector's same-stream notion):
        # with per_fps off, an fps wiggle is not a stream change and must
        # not drop the toast for an apply that really happened.
        profile = session.profile
        if profile is None or not self._same_stream(pending_profile, profile):
            session.pending_notification = None
            return
        session.pending_notification = None
        self._toast(STRING_OFFSET_APPLIED, pending_ms, profile,
                    enabled=self._settings.notify_apply_enabled)
        self._log("AOMe_Notifier: Released pending offset notification after "
                  "stream stabilization")

    def _on_user_offset_saved(self, event):
        if not self._sessions.is_alive(event.session_id):
            return
        # A manual save supersedes any held provisional toast: the user's
        # value is the fact on the ground, and releasing the old held ms on
        # the next stabilization would announce a value that no longer
        # applies. (Legacy parity: its non-suppressed apply path cleared the
        # pending toast before the equivalent sequence could surface it.)
        self._sessions.current.pending_notification = None
        # The payload is the profile/ms captured at store time by the watcher;
        # do NOT re-read session/settings for the message.
        self._toast(STRING_OFFSET_SAVED, event.ms, event.profile,
                    enabled=self._settings.notify_learn_enabled)

    def _on_unsaved_discarded(self, event):
        if not self._sessions.is_alive(event.session_id):
            return
        # Save-related feedback, so it lives under the LEARN gate (D3
        # amendment): the user's manual adjustment was discarded by the
        # zero-reset because it never reached the store — the toast is the
        # "why did my offset vanish" answer. Deliberately outside the
        # dedupe window (dedupe_key=None; it fires once per reset by
        # construction) and with English fallbacks (its whole content is
        # the explanation). It rides the fade guard like every toast: a
        # zero-reset lands on stream changes, right where apply/saved
        # toasts fade out — the one-shot explanation must not be swallowed.
        if not self._settings.notify_learn_enabled():
            return
        title = self._gui.localized(STRING_OFFSET_NOT_SAVED) or (
            "Offset not saved")
        message = self._gui.localized(STRING_RESET_BASELINE) or (
            "Reset to 0 ms. Nothing is stored for this stream")
        self._log(f"AOMe_Notifier: {title} — discarded unstored "
                  f"{event.ms}ms for {event.profile.describe()}")
        self._present(message, self._settings.notification_duration_ms(),
                      title=title, dedupe_key=None,
                      enabled=self._settings.notify_learn_enabled)

    def _on_store_corrupted(self, _event):
        # An error notice, not a per-kind toast: deliberately outside the
        # notify_apply/notify_learn gates (enabled=None — must never be
        # muted, so the deferred release re-checks nothing) and the dedupe
        # window (dedupe_key=None; it fires once per quarantine, has no
        # session). It still flows through the choke point so its 7s
        # window is stamped and the first apply toast of the playback
        # cannot ride its fade-out. localized() degrades to '' on a
        # transient failure, and this is the user's ONLY signal that
        # stored offsets were reset — fall back to the English source
        # string rather than raising a blank toast.
        message = self._gui.localized(STRING_STORE_CORRUPTED) or (
            "Stored offsets were unreadable and were reset "
            "(backup kept as offsets.json.bad)")
        self._log("AOMe_Notifier: surfaced store corruption notice")
        self._present(message, CORRUPTION_NOTICE_MS,
                      title=None, dedupe_key=None, enabled=None)

    def _on_raise_toast(self, event):
        # The fade-guarded release. Dedupe and the guard were decided at
        # request time and cannot have gone stale (an immediate raise cancels
        # this timer; a contender key-replaces it), but the per-kind gate is
        # a live setting and must be re-checked at fire time — it rides on
        # the event, so the release re-gates under its OWN kind's toggle
        # (D10), never another's. None = an ungated notice.
        if event.enabled is not None and not event.enabled():
            return
        self._raise(event.message, event.duration_ms, event.title,
                    event.dedupe_key)

    # -- internals --------------------------------------------------------------

    def _same_stream(self, held, current):
        """Offset-relevant identity at the granularity in force RIGHT NOW."""
        per_fps = self._settings.per_fps_offsets_enabled()
        return (policies.stream_identity(held, per_fps)
                == policies.stream_identity(current, per_fps))

    def _toast(self, string_id, ms, profile, *, enabled):
        # D10: each toast kind is gated by its own toggle, so muting the
        # routine apply announcements never silences the learn feedback
        # (or vice versa). ``enabled`` is the gate READ (the bound settings
        # accessor, evaluated here at fire time), passed by the call site —
        # which knows its kind statically — so a future toast kind can never
        # silently inherit another kind's toggle (E3 review).
        if not enabled():
            return

        now = self._clock()
        # Dedupe at the offset-relevant granularity: with per_fps off an
        # fps wiggle must not defeat the window and re-toast a duplicate.
        per_fps = self._settings.per_fps_offsets_enabled()
        key = self._dedupe_key(string_id, ms, profile, per_fps)
        if self._last_raise is not None:
            last_key, last_at, _ = self._last_raise
            if key == last_key and now - last_at < self.DEDUPE_SECONDS:
                return

        # Toast shape (E7 field fix, beta1 on Windows): the saved/applied
        # line rides as the toast TITLE and the profile summary is the
        # whole message — packing both into the message with a newline made
        # Kodi's single-line label auto-scroll (perceived as flashing) and
        # truncate the codec off the end. The rate is shown only when it is
        # offset-relevant (per_fps ON): with the toggle off the value lives
        # under the all-rates key, and "23.976 fps" would both mislead and
        # crowd out the codec. Rendered HERE, at request time: a per_fps
        # flip inside a deferral re-resolves and posts a fresh OffsetApplied
        # that key-replaces the deferred toast, so the payload can stay the
        # fact it described.
        sign = '+' if ms > 0 else ''
        heading = f"{self._gui.localized(string_id)}: {sign}{ms} ms"
        summary = store_keys.profile_summary(
            profile.hdr_type, profile.audio_format,
            profile.video_fps if per_fps else None)
        self._present(summary, self._settings.notification_duration_ms(),
                      title=heading, dedupe_key=key, enabled=enabled)

    def _present(self, message, duration_ms, *, title, dedupe_key, enabled):
        # The single raise choke point: EVERY notifier toast flows through
        # the fade guard here, so each raise is visible to the next one's
        # band check whatever kind it is (the module docstring has the
        # coverage doctrine).
        delay = self._fade_guard_delay(self._clock())
        if delay > 0.0:
            self._dispatcher.schedule(
                delay,
                events.RaiseToast(message=message, title=title,
                                  duration_ms=duration_ms,
                                  dedupe_key=dedupe_key, enabled=enabled),
                key=self._FADE_KEY)
            self._log(f"AOMe_Notifier: deferring toast {delay * 1000:.0f}ms "
                      f"past the previous toast's fade-out")
            return
        self._raise(message, duration_ms, title, dedupe_key)

    def _fade_guard_delay(self, now):
        """Seconds to wait so this toast misses the previous toast's fade.

        Zero (raise immediately) unless the toast would land inside
        [shown, shown + FADE_GUARD_SECONDS] after our last raise, where
        ``shown`` is the display time the last toast was given, floored at
        Kodi's internal clamp: earlier arrivals are in-place swaps on the
        still-open window, later ones reopen it fresh (the module docstring
        has the Kodi mechanics).
        """
        if self._last_raise is None:
            return 0.0
        _, last_at, last_duration_ms = self._last_raise
        shown_s = max(last_duration_ms, self.KODI_MIN_DISPLAY_MS) / 1000.0
        elapsed = now - last_at
        if elapsed < shown_s or elapsed >= shown_s + self.FADE_GUARD_SECONDS:
            return 0.0
        return shown_s + self.FADE_GUARD_SECONDS - elapsed

    def _raise(self, message, duration_ms, title, dedupe_key):
        # This raise makes any pending deferred release stale by definition —
        # the fresher fact is taking the window. (No-op when we ARE the
        # deferred release: its timer was consumed before dispatch.)
        self._dispatcher.cancel(self._FADE_KEY)
        self._gui.notification(message, duration_ms, title=title)
        self._log(f"AOMe_Notifier: {title + ' — ' if title else ''}{message}")
        self._last_raise = (dedupe_key, self._clock(), duration_ms)

    @staticmethod
    def _dedupe_key(string_id, ms, profile, per_fps):
        # Offset-toast dedupe identity. _toast's single per_fps read feeds
        # this key AND the rendered summary, so the two can never disagree;
        # the notices are outside dedupe by design (dedupe_key=None, which
        # a real tuple key never equals).
        return (string_id, policies.stream_identity(profile, per_fps), ms)
