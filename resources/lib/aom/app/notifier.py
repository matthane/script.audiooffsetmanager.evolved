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
# "Offset not saved" / "Reset to 0 ms — nothing stored for this stream":
# the zero-reset discarded a manual adjustment that never reached the
# store (D3 amendment, E7).
STRING_OFFSET_NOT_SAVED = 32132
STRING_RESET_BASELINE = 32133


class Notifier:
    """Owns offset toasts: deferral-until-stable and the 1s dedupe window."""

    DEDUPE_SECONDS = 1.0

    def __init__(self, dispatcher, session_tracker, settings, gui,
                 clock=time.monotonic, *, log_debug):
        self._sessions = session_tracker
        self._settings = settings
        self._gui = gui
        self._clock = clock
        self._log = log_debug
        # Dedupe state: (string_id, profile identity, ms) + monotonic stamp.
        self._last_toast = None
        self._last_toast_at = None

        dispatcher.subscribe(events.OffsetApplied, self._on_offset_applied)
        dispatcher.subscribe(events.UserOffsetSaved, self._on_user_offset_saved)
        dispatcher.subscribe(events.StreamStabilized, self._on_stream_stabilized)
        dispatcher.subscribe(events.StoreCorrupted, self._on_store_corrupted)
        dispatcher.subscribe(events.UnsavedOffsetDiscarded,
                             self._on_unsaved_discarded)

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
        # dedupe window (it fires once per reset by construction) and with
        # English fallbacks (its whole content is the explanation).
        if not self._settings.notify_learn_enabled():
            return
        title = self._gui.localized(STRING_OFFSET_NOT_SAVED) or (
            "Offset not saved")
        message = self._gui.localized(STRING_RESET_BASELINE) or (
            "Reset to 0 ms — nothing stored for this stream")
        self._gui.notification(message,
                               self._settings.notification_duration_ms(),
                               title=title)
        self._log(f"AOMe_Notifier: {title} — discarded unstored "
                  f"{event.ms}ms for {event.profile.describe()}")

    def _on_store_corrupted(self, _event):
        # An error notice, not a per-kind toast: deliberately outside the
        # notify_apply/notify_learn gates and the dedupe window (it fires
        # once per quarantine, has no session, and must never be muted).
        # localized() degrades to '' on a transient failure, and this is
        # the user's ONLY signal that stored offsets were reset — fall back
        # to the English source string rather than raising a blank toast.
        message = self._gui.localized(STRING_STORE_CORRUPTED) or (
            "Stored offsets were unreadable and were reset "
            "(backup kept as offsets.json.bad)")
        self._gui.notification(message, CORRUPTION_NOTICE_MS)
        self._log("AOMe_Notifier: surfaced store corruption notice")

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
        per_fps = self._settings.per_fps_offsets_enabled()
        # Dedupe at the same offset-relevant granularity: with per_fps off
        # an fps wiggle must not defeat the window and re-toast a duplicate.
        identity = policies.stream_identity(profile, per_fps)
        key = (string_id, identity, ms)
        # _last_toast and _last_toast_at are set in lockstep, and a real key
        # (a tuple) never equals the None sentinel, so the key comparison
        # alone guards the subtraction.
        if key == self._last_toast and \
                now - self._last_toast_at < self.DEDUPE_SECONDS:
            return

        # Toast shape (E7 field fix, beta1 on Windows): the saved/applied
        # line rides as the toast TITLE and the profile summary is the
        # whole message — packing both into the message with a newline made
        # Kodi's single-line label auto-scroll (perceived as flashing) and
        # truncate the codec off the end. The rate is shown only when it is
        # offset-relevant (per_fps ON): with the toggle off the value lives
        # under the all-rates key, and "23.976 fps" would both mislead and
        # crowd out the codec.
        sign = '+' if ms > 0 else ''
        heading = f"{self._gui.localized(string_id)}: {sign}{ms} ms"
        summary = store_keys.profile_summary(
            profile.hdr_type, profile.audio_format,
            profile.video_fps if per_fps else None)

        self._gui.notification(summary,
                               self._settings.notification_duration_ms(),
                               title=heading)
        self._log(f"AOMe_Notifier: {heading} — {summary}")
        self._last_toast = key
        self._last_toast_at = now
