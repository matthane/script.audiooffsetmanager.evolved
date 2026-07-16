"""Offset application: gate via policy, resolve via the store, apply, announce.

The apply half of the legacy OffsetManager (its notification half became the
Notifier). One decision path, two triggers:

- ``ProfileChanged`` — the detector adopted a (new) complete profile: the
  apply trigger. NOT ``PlaybackStarted``: the profile is always None at AV
  start (discovery has not run), so an apply there could only skip.
- ``StreamStabilized`` — the retry edge: a failed apply RPC is retried on
  the next stabilization, and the ``session.applied`` dedupe makes the
  common already-applied case a no-op.

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
The forced 0 consumes the markers (one-shot), posts a typed
``DeletedProfileReset`` when a nonzero value was actually wiped (the
Notifier's confirmation toast), and stays silent when the delay was
already 0. A marker on a key consulted BEFORE a hit (deleted exact entry,
kept ``all`` fallback) is consumed silently after the hit applies — the
fallback the user kept overwrites the residue anyway.

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

    # -- triggers (dispatcher thread) --------------------------------------------

    def _on_profile_changed(self, event):
        """Detector adopted a (new) profile: the apply trigger."""
        self._apply(event.session_id)

    def _on_stream_stabilized(self, event):
        """Retry edge: re-run the apply; the dedupe no-ops the common case."""
        self._apply(event.session_id)

    # -- the apply -----------------------------------------------------------------

    def _apply(self, session_id):
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
            if resolution.reset_keys:
                self._reset_deleted(session, profile, resolution.reset_keys)
            else:
                self._reset_if_owned(session, profile)
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
        retries naturally.
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
        # An unreadable delay resets silently — never toast on a hiccup.
        if current_ms is not None:
            self._dispatcher.post(events.DeletedProfileReset(
                session_id=session.session_id, profile=profile,
                ms=current_ms))

    def _consume_markers(self, reset_keys):
        for key in reset_keys:
            self._offsets.consume_reset(key)

    def _reset_if_owned(self, session, profile):
        """The miss policy's second half (D3 amendment, E7 field call).

        ``session.applied is None`` means the addon has not touched this
        session: whatever delay exists belongs to the user or to Kodi's
        per-file memory, and a fresh/untaught profile must never clobber
        it (P1). Once we HAVE acted, the delay in force was set for the
        PREVIOUS profile — stale residue under the per-profile model — so
        an unlearned profile returns to Kodi's 0 baseline. The reset is
        idempotent: a delay already reading 0 is left alone, which also
        makes the retry pass on re-stabilization a no-op.
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
