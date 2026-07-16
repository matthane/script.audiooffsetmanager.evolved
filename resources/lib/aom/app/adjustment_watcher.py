"""Adjustment watching: poll the audio-delay infolabel, store user changes.

Replaces the legacy ``ActiveMonitor`` — a dedicated thread that watched the
Kodi audio-settings/OSD-slider dialog IDs (10124/10145) and, on dialog close,
read the slider value and stored it. That dialog-ID coupling had two costs:
it needed its own thread (and the cross-thread profile read that came with
it), and it saw ONLY adjustments made through the OSD. A change made with a
keymap, a remote app, or a JSON-RPC ``Player.SetAudioDelay`` never opened a
dialog, so the legacy monitor's ``audio_settings_open`` / ``slider_was_open``
state machine never fired and the change was silently lost.

The new design polls ``Player.AudioDelay`` on the dispatcher thread via
self-scheduled ``WatchTick`` events. No threads, no dialog IDs, no
open/close state machine — every source of an adjustment is caught because
we watch the VALUE, not the GUI that (sometimes) sets it.

Eligibility (``_eligible``) is deliberately minimal: a profile exists,
learning is on ("remember manual adjustments" — the promoted core of the
product, P2), and the addon is not paused (D9). No axis-gating happens here
— the store path (``_store``) re-validates the WHOLE profile
(``policies.is_complete``) before writing, so an incomplete stream is
watched but never stored.

Baseline rule: ``session.watch_baseline_ms`` is the last delay value we have
ACCOUNTED FOR (our own apply, or a value already stored). Only a CHANGE away
from the baseline observed WHILE watching can become a user adjustment. The
first non-ours value a session sees is ADOPTED as the baseline silently,
never stored — this is the failed-RPC-leftover guard: a delay left behind by
an apply RPC that failed, or pre-existing player state, must not be written
over the user's configured offset.

Quiescence replaces the legacy "slider closed" moment: a foreign value must
hold unchanged for ``QUIESCENCE_SECONDS`` before it is stored (the tick
cadence tightens to ``ACTIVE_TICK_SECONDS`` while a candidate is pending).
Two teardown-phantom defenses back this up (Phase 8 field bug, 2026-07-15):
during a slow stop — PM4K flows and natural end-of-media on CoreELEC
measured 0.3-1.15s — Kodi's delay infolabel reads a parseable 0 while the
session is still alive, which is indistinguishable from a user dialing to
0. ``QUIESCENCE_SECONDS`` is sized to outrun that window, and the store
path re-checks ``gateway.active_player_id()`` at store time, discarding
the observation chain when the player is already gone (a native stop
empties the player list before ``OnStop`` even fires).
Acknowledged trade-off: without a dialog-close edge we cannot know the user
is "done", so we wait out a short quiet window instead — a user who dials
through several values only stores the one they settle on, and an
adjust-back-to-the-original before quiescence stores nothing.

Self-echo suppression: an automatic apply is a JSON-RPC player call, so our
own applied value shows up in the infolabel just like a user's would. The
applier records ``session.applied = (store_key, delay_ms)`` BEFORE issuing
the RPC precisely so ``observed == session.applied[1]`` here is always
current; a match is our own value — baseline-refresh, never store. A
corollary (reviewed, accepted): an automatic apply landing INSIDE a pending
quiescence window supersedes the candidate — the pending value was dialed
for the stream that just changed, so its target profile is ambiguous and
dropping beats storing it under the wrong key (the legacy monitor did the
latter — the adopt-vs-store interleaving this design closes).

The classic settings-dialog store deferral is DELETED, not ported: offsets
live in the sparse store file now, not in settings.xml, so the dialog's
save-on-close cannot clobber a store write — that hazard class is gone with
the medium (P4).

Store-time profile derivation on the dispatcher thread closes the legacy
adjust-vs-adopt interleaving: the old monitor thread stored under the live
profile while the detector could re-adopt a different one concurrently. Now
adoption (StreamDetector) and store (here) are serialized on ONE thread and
the write key is derived from ``session.profile`` + the live ``per_fps``
toggle at the store instant (D4: one rule, never conditional on lookup
history — the OffsetTable routes through ``resolve.write_key``).

On a successful store the watcher posts a session-stamped
``UserOffsetSaved`` (profile + ms + resolved key captured at store time),
the typed replacement for the legacy unstamped ``USER_ADJUSTMENT`` signal.

Pure app layer: Kodi I/O via the injected gateway, eligibility reads via the
injected settings adapter, offset reads/writes via the injected OffsetTable
(the store adapter — key derivation is its concern), log sinks injected;
no Kodi imports.
"""

import time

from resources.lib.aom.app import events
from resources.lib.aom.domain import policies


class AdjustmentWatcher:
    """Polls the audio-delay infolabel; stores quiesced user adjustments."""

    IDLE_TICK_SECONDS = 1.0     # poll cadence when nothing is happening
    ACTIVE_TICK_SECONDS = 0.25  # tightened cadence while observing a change
    # Foreign value must hold this long to be stored. 2.0s (raised from 1.0)
    # outruns the teardown phantom: field-measured stop windows where the
    # delay infolabel reads a parseable 0 while the session is still alive
    # ran 0.3-1.15s (Phase 8, CoreELEC/PM4K 2026-07-15; a 1.15s window beat
    # the old 1.0s quiescence and stored 0 over the user's offset).
    QUIESCENCE_SECONDS = 2.0
    INFOLABEL_AUDIO_DELAY = 'Player.AudioDelay'
    _TICK_KEY = 'aom.watcher.tick'

    def __init__(self, dispatcher, session_tracker, gateway, settings,
                 offsets, clock=time.monotonic, *, log_debug, log_warning):
        self._dispatcher = dispatcher
        self._sessions = session_tracker
        self._gateway = gateway
        self._settings = settings
        self._offsets = offsets      # OffsetTable: get/store by profile
        self._clock = clock
        self._log = log_debug
        self._warn = log_warning

        dispatcher.subscribe(events.ProfileChanged, self._on_profile_changed)
        dispatcher.subscribe(events.SettingsChanged, self._on_settings_changed)
        dispatcher.subscribe(events.WatchTick, self._on_watch_tick)
        dispatcher.subscribe(events.PlaybackStopped, self._on_playback_ended)
        dispatcher.subscribe(events.PlaybackEnded, self._on_playback_ended)

    # -- eligibility ------------------------------------------------------------

    def _eligible(self, profile):
        """Watch when a profile exists, learning is on, and not paused.

        The classic per-HDR enable and hdr/fps unknown-checks are gone with
        their features: the HDR axis always resolves under the open
        vocabulary (sdr default), and completeness is the STORE path's
        concern (_store re-validates the whole profile before writing, so
        an incomplete stream is watched but never persisted — classic
        parity for the audio-unknown case, now uniform).
        """
        return (profile is not None
                and self._settings.remember_adjustments_enabled()
                and not self._settings.pause_enabled()
                # A permanently unwritable store (newer-schema file after a
                # downgrade) must stop the learn loop outright — otherwise
                # every quiescence cycle re-detects and re-fails the same
                # adjustment forever (E2 review finding).
                and not self._offsets.read_only)

    # -- eligibility triggers (dispatcher thread) -------------------------------

    def _on_profile_changed(self, event):
        if not self._sessions.is_alive(event.session_id):
            return
        session = self._sessions.current
        # A (re)adoption makes any in-flight observation ambiguous: a pending
        # candidate was dialed against the PREVIOUS profile (storing it under
        # the new key would be the adopt-vs-store hazard), and the baseline
        # belongs to that profile's episode too. Drop both; the next tick
        # re-establishes them — the applier (ordered before us) has already
        # recorded its apply, so our own value reads as self-echo.
        self._clear_observation(session)
        self._evaluate(session)

    def _on_settings_changed(self, _event):
        session = self._sessions.current
        if session is None:
            return
        self._evaluate(session)

    def _evaluate(self, session):
        if self._eligible(session.profile):
            # key-replace keeps exactly one live chain, so re-evaluating
            # (ProfileChanged + SettingsChanged in quick succession) is
            # idempotent — never spawns a second watch loop.
            self._schedule_tick(session.session_id, self.IDLE_TICK_SECONDS)
        else:
            self._dispatcher.cancel(self._TICK_KEY)
            self._clear_observation(session)
            self._log(f"AOM_AdjustmentWatcher: not watching session "
                      f"#{session.session_id} (ineligible: "
                      f"profile={session.profile})")

    def _on_playback_ended(self, _event):
        self._dispatcher.cancel(self._TICK_KEY)

    # -- the poll (dispatcher thread) -------------------------------------------

    def _on_watch_tick(self, event):
        if not self._sessions.is_alive(event.session_id):
            return  # a superseded session's chain is inert
        session = self._sessions.current
        if not self._eligible(session.profile):
            self._clear_observation(session)
            self._log("AOM_AdjustmentWatcher: no longer eligible; stopping "
                      "watch")
            return  # ProfileChanged/SettingsChanged restart the chain
        # One poll, one reschedule: _observe classifies the reading and only
        # picks the next cadence — every continue-watching path funnels here.
        self._schedule_tick(session.session_id, self._observe(session))

    def _observe(self, session):
        """Classify the current delay reading; return the next tick cadence."""
        observed = policies.parse_delay_ms(
            self._gateway.infolabel(self.INFOLABEL_AUDIO_DELAY))
        if observed is None:
            self._log("AOM_AdjustmentWatcher: audio delay unreadable; "
                      "retrying")
            return self.IDLE_TICK_SECONDS

        applied_ms = session.applied[1] if session.applied is not None else None

        if observed == applied_ms:
            # Our own apply echoing back (the applier records session.applied
            # BEFORE the RPC, so this comparison is always current).
            session.watch_baseline_ms = observed
            session.watch_pending = None
            return self.IDLE_TICK_SECONDS

        if session.watch_baseline_ms is None:
            # First observation and it isn't ours: adopt as baseline silently.
            # Never store a value we merely found (failed-apply leftover or
            # pre-existing player state) — only a CHANGE while watching is a
            # user adjustment.
            session.watch_baseline_ms = observed
            self._log(f"AOM_AdjustmentWatcher: adopting baseline "
                      f"{observed}ms (first observation)")
            return self.IDLE_TICK_SECONDS

        if observed == session.watch_baseline_ms:
            # Nothing changed, or the user dialed back to the baseline before
            # quiescence ("adjust back to what it was" stores nothing).
            session.watch_pending = None
            return self.IDLE_TICK_SECONDS

        # A foreign CHANGE away from the baseline: a quiescence candidate.
        now = self._clock()
        pending = session.watch_pending
        if pending is None or pending[0] != observed:
            session.watch_pending = (observed, now)
            self._log(f"AOM_AdjustmentWatcher: observing manual adjustment "
                      f"{observed}ms; awaiting quiescence")
            return self.ACTIVE_TICK_SECONDS
        if now - pending[1] < self.QUIESCENCE_SECONDS:
            return self.ACTIVE_TICK_SECONDS
        if self._gateway.active_player_id() == -1:
            # Teardown phantom guard (Phase 8 field bug): during a slow stop
            # the delay infolabel can read a parseable 0 while PlaybackStopped
            # hasn't landed yet, so the quiesced "adjustment" belongs to a
            # dying player. Never store an adjustment for a player that no
            # longer exists — discard the whole observation chain (the
            # baseline is teardown-tainted too).
            self._clear_observation(session)
            self._log("AOM_AdjustmentWatcher: no active player at store "
                      "time; discarding pending adjustment")
            return self.IDLE_TICK_SECONDS
        self._store(session, observed)
        return self.IDLE_TICK_SECONDS

    # -- store (dispatcher thread) ----------------------------------------------

    def _store(self, session, observed_ms):
        session.watch_pending = None
        # Read the profile FRESH at store time, on the dispatcher thread — the
        # write key is derived from whatever profile (and toggle value) is in
        # force NOW (see the module docstring's store-time-derivation note).
        profile = session.profile
        if not policies.is_complete(profile):
            # Watched but not persistable: account for the value so we don't
            # chase it, but never write an incomplete key.
            self._log(f"AOM_AdjustmentWatcher: profile incomplete "
                      f"({profile}); not storing {observed_ms}ms")
            session.watch_baseline_ms = observed_ms
            return

        write_key = self._offsets.write_key(profile)
        if write_key is None:
            # Cannot compose a key (unparseable fps under per-fps): account,
            # never persist. is_complete makes this unreachable in practice;
            # the guard keeps the invariant local.
            self._log(f"AOM_AdjustmentWatcher: no write key for {profile}; "
                      f"not storing {observed_ms}ms")
            session.watch_baseline_ms = observed_ms
            return

        if self._offsets.stored_ms_at(write_key) == observed_ms:
            # Already the stored value (e.g. re-dialed to the configured
            # offset): account for it, emit nothing.
            session.watch_baseline_ms = observed_ms
            self._log(f"AOM_AdjustmentWatcher: {observed_ms}ms already stored "
                      f"for {write_key}; nothing to do")
            return

        # store() re-derives the key internally; both derivations run inside
        # this one handler on the one dispatcher thread, so no settings
        # change can interleave — they are the same key by construction.
        stored_key = self._offsets.store(profile, observed_ms)
        if stored_key is None:
            # The value is still foreign; leave the baseline untouched so the
            # next quiescence cycle retries the store.
            self._warn(f"AOM_AdjustmentWatcher: failed to store "
                       f"{observed_ms}ms for {write_key}")
            return

        session.watch_baseline_ms = observed_ms
        # The user's value is now the applied value too, so the applier's
        # dedupe guard stays honest.
        session.applied = (stored_key, observed_ms)
        # The store just changed: any remembered miss-chain is stale (a
        # delete->re-teach->delete cycle must re-log its miss, not be
        # swallowed by session-lifetime dedupe — E2 review finding).
        session.miss_announced = None
        self._log(f"AOM_AdjustmentWatcher: Stored audio offset "
                  f"{observed_ms}ms for {stored_key}")
        self._dispatcher.post(events.UserOffsetSaved(
            session_id=session.session_id, profile=profile, ms=observed_ms,
            key=stored_key))

    # -- internals --------------------------------------------------------------

    def _clear_observation(self, session):
        """Drop ALL observation state whenever the watch chain stops.

        The baseline must not survive a not-watching gap: a delay changed
        while monitoring was disabled would otherwise compare against the
        stale baseline on re-enable and be stored as a fresh adjustment —
        but only a change observed WHILE watching is an adjustment. Clearing
        makes the first post-gap observation re-adopt silently (exactly the
        fresh state a restarted legacy monitor had).
        """
        session.watch_pending = None
        session.watch_baseline_ms = None

    def _schedule_tick(self, session_id, delay):
        """One place for the self-scheduled poll chain (key-replaced)."""
        self._dispatcher.schedule(
            delay, events.WatchTick(session_id=session_id), key=self._TICK_KEY)
