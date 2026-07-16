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
exact/fallback/miss. **A miss is a no-op** (D3): no RPC, Kodi's delay stays
untouched, and one debug line per distinct consulted chain — never a spam
stream (``session.miss_announced`` dedupes repeats within an episode).

Pure app layer: Kodi I/O via the injected gateway, settings via the injected
adapter, log sinks injected; no Kodi imports.
"""

from resources.lib.aom.app import events
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
            self._log("AOM_OffsetApplier: No valid player ID found to set "
                      "audio delay")
            return

        resolution = self._offsets.resolve(profile)
        if resolution.entry is None:
            # D3: a miss applies NOTHING — Kodi's delay stays untouched.
            # One debug line per distinct consulted chain, not per event.
            if session.miss_announced != resolution.tried:
                session.miss_announced = resolution.tried
                self._log(f"AOM_OffsetApplier: no stored offset for "
                          f"{profile.describe()} (tried "
                          f"{', '.join(resolution.tried)}); leaving Kodi's "
                          f"delay untouched")
            return

        key = resolution.key
        delay_ms = resolution.ms

        if session.applied == (key, delay_ms):
            self._log(f"AOM_OffsetApplier: Offset already applied for "
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
            self._warn(f"AOM_OffsetApplier: audio delay RPC failed for "
                       f"{key}; will retry on the next stabilization")
            return

        self._log(f"AOM_OffsetApplier: Applied {delay_ms}ms for {key} "
                  f"(hit={resolution.hit_kind}, provisional={provisional}); "
                  f"{session.describe()}")
        self._dispatcher.post(events.OffsetApplied(
            session_id=session.session_id, profile=profile, ms=delay_ms,
            provisional=provisional))

    def _should_apply(self, profile):
        """Resolve the inputs and log the reason; the decision is the policy's."""
        allowed, reason = policies.should_apply(
            profile, paused=self._settings.pause_enabled())
        if allowed:
            return True

        if reason == 'paused':
            self._log("AOM_OffsetApplier: paused; skipping audio offset "
                      "application")
        elif reason == 'no_profile':
            self._log("AOM_OffsetApplier: No stream profile available; "
                      "skipping offset")
        elif reason == 'unknown_format':
            self._log(f"AOM_OffsetApplier: Skipping audio offset - profile "
                      f"incomplete ({profile.describe()})")
        return False
