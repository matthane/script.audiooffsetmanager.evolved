"""Offset application: gate via policy, resolve via the store, apply, announce.

The apply half of the legacy OffsetManager (its notification half became the
Notifier). One decision path, four triggers:

- ``ProfileChanged`` — the detector adopted a (new) complete profile: the
  apply trigger. NOT ``PlaybackStarted``: the profile is always None at AV
  start (discovery has not run), so an apply there could only skip.
- ``StreamStabilized`` — the retry edge: a failed apply RPC is retried on
  the next stabilization, and the ``session.applied`` dedupe makes the
  common already-applied case a no-op.
- ``SettingsChanged`` — the immediate-effect edge (E7): every input to the
  decision is already read fresh at decision instant (the ``per_fps``
  toggle inside the OffsetTable's resolve, the pause gate in the policy),
  so re-running the decision when the user saves the settings dialog makes
  mid-playback edits act NOW instead of at the next stream event, through
  the same gates as any other trigger (a paused addon still does nothing).
  One deliberate divergence from the stream-change triggers: a settings
  save does NOT change the profile, so a foreign delay (the user's hand)
  still targets the stream in force — the miss path's baseline reset is
  therefore withheld when the delay diverged from our last apply
  (``profile_unchanged``), where a stream change would reset it and
  toast. Only our own orphaned residue (a toggle flip stranding the
  value WE applied) is reset by a save. Corollary: an offset the user
  re-dialed while the addon was paused survives un-pausing the same way
  — the dedupe sees the stored value as already applied and leaves the
  user's hand alone until the next stream event. No live session, no
  work.
- ``StoreMutated`` — the management-view edge (E7, user call
  2026-07-16): a delete/clear that actually changed the store is a
  resolve moment too, so deleting the PLAYING profile's offset takes
  effect immediately — the marked miss forces its promised 0 at the
  deletion itself, not at the next playback ("next playback" was only
  ever the fallback for mutations made while nothing plays, where no
  live delay exists to touch). Same shape as SettingsChanged in every
  other respect: profile unchanged, foreign delays preserved, standing
  gates hold, dedupe no-ops mutations that don't touch the live
  resolution.

Both silent reset paths post a session-stamped ``DelayReset`` on a
successful reset RPC: a reset is an automatic delay change exactly like
an apply, so the watcher drops any in-flight observation on it (the
supersede corollary — without this, a candidate dialed just before a
marker-forced reset could quiesce against a lagging infolabel and
re-store the value the user had just deleted).

Contracts (both reviewed and pinned by tests):

- **applied-before-RPC**: ``session.applied`` is recorded BEFORE the
  ``set_audio_delay`` call and restored on failure. The AdjustmentWatcher's
  self-echo suppression compares observed delays against ``session.applied``
  — record-after-success would let it store our own apply as a user
  adjustment. Two flow tests pin this at the RPC boundary; do not reorder.
- **Freshness**: the profile is read from ``session.profile`` at the moment
  of use (the detector, on this same dispatcher thread, is its sole writer)
  — never captured across events; the ``per_fps`` toggle is consulted
  inside the OffsetTable at resolve instant for the same reason.

The apply is *eager*: it runs on adoption, before stability, because A/V
sync matters immediately. It is marked ``provisional`` unless the session is
already STABLE, and the posted ``OffsetApplied(provisional=...)`` lets the
Notifier hold the toast until stabilization. This component never toasts.

Offsets come from the injected ``OffsetTable`` (the sparse-store adapter):
``resolve(profile)`` returns a ``Resolution`` whose ``hit_kind`` is
exact/fallback/miss. **A miss is a no-op until the addon has acted on the
session, then a zero-reset** (D3 as amended at E7): before the first AOM
apply/store, a miss leaves Kodi's delay completely untouched (a fresh
install must never clobber the user's/Kodi's own per-file delay). After
that, the delay in force is AOM-owned or belongs to the PREVIOUS profile,
so an unlearned profile resets it to Kodi's 0 baseline — silently when the
discarded value is our own residue, with a typed
``UnsavedOffsetDiscarded`` (the "Offset not saved" toast) when it diverged
from the last apply, i.e. contained a manual adjustment that never reached
the store. Either way, one debug line per distinct consulted chain — never
a spam stream (``session.miss_announced`` dedupes repeats within an
episode), and the reset is idempotent (a delay already at 0 is left alone).

**Deleted profiles override the wait-until-acted gate** (D3 second
amendment, E7 field decision): a miss whose consulted chain carries reset
markers (``Resolution.reset_keys`` — the user deleted those keys in the
management view) forces the delay to 0 IMMEDIATELY, first action or not.
The user's deletion is the authorization the P1 guard otherwise waits for;
without this, Kodi's per-file memory keeps replaying the deleted offset.
The forced 0 consumes the markers (one-shot) and is completely SILENT —
zero is the implicit, expected outcome of the deletion, so a toast would
be noise (user call, same day as the amendment); the debug line is the
only trace. A marker on a key consulted BEFORE a hit (deleted exact
entry, kept ``all`` fallback) is consumed silently after the hit applies
— the fallback the user kept overwrites the residue anyway.

Pure app layer: Kodi I/O via the injected gateway, settings via the injected
adapter, log sinks injected; no Kodi imports.
"""

from resources.lib.aom.app import events
from resources.lib.aom.app.adjustment_watcher import AdjustmentWatcher
from resources.lib.aom.domain import policies
from resources.lib.aom.domain.stream_state import StreamState


class OffsetApplier:
    """Applies the stored offset for the session's current profile."""

    def __init__(self, dispatcher, session_tracker, gateway, settings,
                 offsets, *, log_debug, log_warning):
        self._dispatcher = dispatcher
        self._sessions = session_tracker
        self._gateway = gateway
        self._settings = settings
        self._offsets = offsets
        self._log = log_debug
        self._warn = log_warning

        dispatcher.subscribe(events.ProfileChanged, self._on_profile_changed)
        dispatcher.subscribe(events.StreamStabilized, self._on_stream_stabilized)
        dispatcher.subscribe(events.SettingsChanged, self._on_settings_changed)
        dispatcher.subscribe(events.StoreMutated, self._on_store_mutated)

    # -- triggers (dispatcher thread) --------------------------------------------

    def _on_profile_changed(self, event):
        """Detector adopted a (new) profile: the apply trigger."""
        self._apply(event.session_id)

    def _on_stream_stabilized(self, event):
        """Retry edge: re-run the apply; the dedupe no-ops the common case."""
        self._apply(event.session_id)

    def _on_settings_changed(self, _event):
        """Immediate-effect edge: a settings save re-runs the decision.

        The event carries no session stamp (it is not session work), so the
        live session is fetched here; none live means nothing to reconcile.
        """
        self._reconcile_live_session()

    def _on_store_mutated(self, _event):
        """Management-view edge: a store-changing delete/clear re-runs the
        decision, so deleting the playing profile's offset acts NOW (the
        marked miss forces its 0 at the deletion itself)."""
        self._reconcile_live_session()

    def _reconcile_live_session(self):
        session = self._sessions.current
        if session is None:
            return
        self._apply(session.session_id, profile_unchanged=True)

    # -- the apply -----------------------------------------------------------------

    def _apply(self, session_id, *, profile_unchanged=False):
        if not self._sessions.is_alive(session_id):
            return  # superseded session: the event is inert
        session = self._sessions.current

        # Freshly derived at the moment of use (settings doctrine).
        profile = session.profile
        if not self._should_apply(profile):
            return

        if profile.player_id == -1:
            self._log("AOMe_OffsetApplier: No valid player ID found to set "
                      "audio delay")
            return

        resolution = self._offsets.resolve(profile)
        if resolution.entry is None:
            # D3 (amended): one debug line per distinct consulted chain,
            # then the miss policy — untouched before the addon's first
            # action of the session, zero-reset after; a chain carrying
            # reset markers forces the 0 regardless (second amendment).
            if session.miss_announced != resolution.tried:
                session.miss_announced = resolution.tried
                self._log(f"AOMe_OffsetApplier: no stored offset for "
                          f"{profile.describe()} (tried "
                          f"{', '.join(resolution.tried)})")
            # A held provisional "applied X" toast cannot survive a miss
            # resolution: whatever the reset paths decide, X is no longer
            # the value this profile stands to announce.
            session.pending_notification = None
            if resolution.reset_keys:
                self._reset_deleted(session, profile, resolution.reset_keys)
            else:
                self._reset_if_owned(session, profile,
                                     profile_unchanged=profile_unchanged)
            return

        key = resolution.key
        delay_ms = resolution.ms

        if session.applied == (key, delay_ms):
            self._log(f"AOMe_OffsetApplier: Offset already applied for "
                      f"{key} at {delay_ms}ms; skipping duplicate apply")
            return

        provisional = session.stream_state is not StreamState.STABLE

        # Bookkeeping BEFORE the RPC (watcher self-echo contract — see the
        # module docstring). Restored on failure so the dedupe guard cannot
        # block the retry.
        previous_applied = session.applied
        session.applied = (key, delay_ms)
        if not self._gateway.set_audio_delay(profile.player_id,
                                             delay_ms / 1000.0):
            session.applied = previous_applied
            self._warn(f"AOMe_OffsetApplier: audio delay RPC failed for "
                       f"{key}; will retry on the next stabilization")
            return

        self._log(f"AOMe_OffsetApplier: Applied {delay_ms}ms for {key} "
                  f"(hit={resolution.hit_kind}, provisional={provisional}); "
                  f"{session.describe()}")
        # A stale marker under a hit (deleted exact entry, kept fallback):
        # the applied value overwrote any residue, so the marker's job is
        # done — consume silently.
        self._consume_markers(resolution.reset_keys)
        self._dispatcher.post(events.OffsetApplied(
            session_id=session.session_id, profile=profile, ms=delay_ms,
            provisional=provisional))

    def _reset_deleted(self, session, profile, reset_keys):
        """Force the 0 a deletion promised (D3 second amendment).

        Runs on a miss whose consulted chain carries reset markers,
        BYPASSING the ``session.applied`` gate: the user's delete in the
        management view is the authorization P1 otherwise waits for. The
        forced 0 is one-shot — markers are consumed on success and on the
        already-0 case; a failed RPC keeps them so the next stabilization
        retries naturally. Silent by design: 0 is the implicit, expected
        outcome of the deletion, so no toast fires (user call, E7).
        """
        raw = self._gateway.infolabel(AdjustmentWatcher.INFOLABEL_AUDIO_DELAY)
        current_ms = policies.parse_delay_ms(raw)
        if current_ms == 0:
            # Nothing visible to do; the marker is spent all the same.
            self._consume_markers(reset_keys)
            return

        # applied-before-RPC contract, same as every other apply path.
        previous_applied = session.applied
        session.applied = (None, 0)
        if not self._gateway.set_audio_delay(profile.player_id, 0.0):
            session.applied = previous_applied
            self._warn("AOMe_OffsetApplier: deleted-profile reset RPC "
                       "failed; will retry on the next stabilization")
            return

        self._consume_markers(reset_keys)
        self._log(f"AOMe_OffsetApplier: reset delay to 0ms for deleted "
                  f"{profile.describe()} (was "
                  f"{'unreadable' if current_ms is None else current_ms}ms; "
                  f"markers {', '.join(reset_keys)})")
        self._dispatcher.post(events.DelayReset(
            session_id=session.session_id))

    def _consume_markers(self, reset_keys):
        for key in reset_keys:
            self._offsets.consume_reset(key)

    def _reset_if_owned(self, session, profile, *, profile_unchanged=False):
        """The miss policy's second half (D3 amendment, E7 field call).

        ``session.applied is None`` means the addon has not touched this
        session: whatever delay exists belongs to the user or to Kodi's
        per-file memory, and a fresh/untaught profile must never clobber
        it (P1). Once we HAVE acted, the delay in force was set for the
        PREVIOUS profile — stale residue under the per-profile model — so
        an unlearned profile returns to Kodi's 0 baseline. The reset is
        idempotent: a delay already reading 0 is left alone, which also
        makes the retry pass on re-stabilization a no-op.

        ``profile_unchanged`` (the settings-save and store-mutation
        triggers) withholds the reset when the delay DIVERGED from our
        last apply: those triggers change no profile, so a foreign value
        (the user's in-flight dial, or a deliberate session-local value
        with remember off) still targets the stream in force — wiping it
        because an unrelated knob was saved or an unrelated entry deleted
        would clobber the user's hand (P1's spirit). Only our own
        residue, orphaned by the trigger itself (a per-fps flip stranding
        the value WE applied), is reset. An unreadable delay is also left
        alone on this path — never act on a hiccup when nothing changed
        underneath.
        """
        if session.applied is None:
            return

        raw = self._gateway.infolabel(AdjustmentWatcher.INFOLABEL_AUDIO_DELAY)
        current_ms = policies.parse_delay_ms(raw)
        if current_ms == 0:
            return

        # Divergence from the last apply means the value being discarded
        # contains a manual adjustment that never reached the store
        # (remember off, or a stream change inside the quiescence window).
        # An unreadable delay resets silently — never toast on a hiccup.
        discarded = None
        if current_ms is not None and current_ms != session.applied[1]:
            discarded = current_ms

        if profile_unchanged and (current_ms is None or discarded is not None):
            shown = 'unreadable' if current_ms is None else f"{current_ms}ms"
            self._log(f"AOMe_OffsetApplier: leaving foreign delay ({shown}) "
                      f"in place for unlearned {profile.describe()} "
                      f"(profile unchanged by this trigger; not ours to "
                      f"reset)")
            return

        previous_applied = session.applied
        session.applied = (None, 0)
        if not self._gateway.set_audio_delay(profile.player_id, 0.0):
            session.applied = previous_applied
            self._warn("AOMe_OffsetApplier: baseline reset RPC failed; "
                       "will retry on the next stabilization")
            return

        self._log(f"AOMe_OffsetApplier: reset delay to 0ms for unlearned "
                  f"{profile.describe()} (was "
                  f"{'unreadable' if current_ms is None else current_ms}ms)")
        self._dispatcher.post(events.DelayReset(
            session_id=session.session_id))
        if discarded is not None:
            self._dispatcher.post(events.UnsavedOffsetDiscarded(
                session_id=session.session_id, profile=profile,
                ms=discarded))

    def _should_apply(self, profile):
        """Resolve the inputs and log the reason; the decision is the policy's."""
        allowed, reason = policies.should_apply(
            profile, paused=self._settings.pause_enabled())
        if allowed:
            return True

        if reason == 'paused':
            self._log("AOMe_OffsetApplier: paused; skipping audio offset "
                      "application")
        elif reason == 'no_profile':
            self._log("AOMe_OffsetApplier: No stream profile available; "
                      "skipping offset")
        elif reason == 'unknown_format':
            self._log(f"AOMe_OffsetApplier: Skipping audio offset - profile "
                      f"incomplete ({profile.describe()})")
        return False
