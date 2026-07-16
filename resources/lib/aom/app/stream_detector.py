"""Stream detection: scheduled single-shot probes + whole-profile verification.

Replaces StreamInfo's blocking gather (RPC retry sleeps, formerly on the Kodi
pump / dispatcher thread) and AvChangeFilter's codec-only verify thread.
Patience lives HERE, expressed as budgeted, session-stamped, cancelable
scheduled events — never as sleeps:

- ``PlaybackStarted`` starts a discovery chain: ``ProbeStream(attempt=n)``
  every ~0.5s (jittered) until the profile is complete or the budget runs
  out. The budget (~10s) matches the legacy worst case of rpc_client's two
  stacked 10x0.5s retry loops (player id, then audio codec).
- A complete profile is adopted (this component is the SOLE writer of
  ``session.profile`` and the owner of every stream-state transition), then
  verified: ``VerifyStream`` re-gathers after 1s and requires the WHOLE
  profile — HDR, FPS and audio, not just the codec — to have held before
  marking the session STABLE and posting ``StreamStabilized``.
- A failed verification (profile changed or went incomplete inside the
  window) re-adopts or re-schedules verification instead of stranding the
  session STABILIZING — the recovery edge the legacy filter lacked.
- ``AvChanged`` triggers an immediate single-shot re-probe: unchanged
  profile → ignored (the legacy duplicate-codec filter, strengthened —
  HDR/FPS-only changes, invisible to the codec filter, now re-verify too);
  changed → re-adopt + re-verify; lost → regress to STABILIZING and let the
  verify loop chase it.

Every gather posts ``StreamProbed`` platform facts — log-only observability
now (the PlatformRecorder dissolved with the stored capability flags; P3).

"Same stream" is judged on the OFFSET-RELEVANT identity
(``policies.stream_identity``, consulted at compare instant with the live
``per_fps_offsets`` toggle) — not raw dataclass equality: incidental fields
(player_id, audio_channels, and — when the toggle is off — the fps rate)
can wiggle between gathers (channel-count flicker during passthrough sync,
VFR fps re-reads) without the stream changing for offset purposes. An
identity-equal gather silently refreshes ``session.profile`` (so downstream
readers see fresh incidental fields) with no events and no state change;
comparing raw equality instead would strand verification in a perpetual
re-adopt loop.

Verbatim acceptance (EVOLVED §3.2/§3.5): the audio and HDR axes carry what
Kodi reported, normalized by ``aom.store.keys`` (case-fold/trim; absence to
'unknown'; the sole hlghdr alias). No whitelist, no fps buckets, no per-HDR
override collapse — the per-fps granularity question moved to the store's
lookup/write instant. The HDR chain-of-evidence (primary -> fallback ->
sdr default -> HLG-gamut sniff) is unchanged.

Intentional divergences from legacy (reviewed):
- Offsets now re-apply ~1s EARLIER on mid-play changes: adoption posts
  ``ProfileChanged`` immediately (the apply is provisional; notifications
  still wait for STABLE), where legacy applied only after its 1s debounce.
- HDR/FPS-bucket-only mid-play changes are full change episodes now (offset
  re-apply, notification, and the legacy ON_AV_CHANGE that drives change
  seek-backs); legacy's codec-only filter ignored them entirely, silently
  keeping a stale offset.
- A stream whose profile never completes (budget exhausted) fires no legacy
  AV events at all, so the active monitor is not started for it; legacy
  started the monitor on hdr+fps-only profiles, but its write path
  re-validated the full profile and could never store anything.

Pure app layer: Kodi I/O goes through the injected gateway, settings reads
through the injected facade; no Kodi imports, log sinks are injected.
"""

import math
import random
from dataclasses import dataclass

from resources.lib.aom.app import events
from resources.lib.aom.domain import formats, policies
from resources.lib.aom.domain.profile import StreamProfile
from resources.lib.aom.store import keys


INFOLABEL_FPS = 'Player.Process(videofps)'
INFOLABEL_HDR = 'Player.Process(video.source.hdr.type)'
INFOLABEL_HDR_FALLBACK = 'VideoPlayer.HdrType'
INFOLABEL_GAMUT = 'Player.Process(amlogic.eoft_gamut)'


@dataclass(frozen=True)
class StreamFacts:
    """One detection pass: the derived profile plus platform observations.

    ``hdr_source`` records which branch of the chain-of-evidence produced
    the HDR type ('primary', 'fallback', 'default-sdr', or 'gamut-hlg') —
    surfaced in the probe log line so field logs show WHICH detection path
    fired, replacing legacy StreamInfo's per-branch debug lines.
    """
    profile: StreamProfile
    platform_hdr_full: bool
    advanced_hlg: bool
    gamut_info: str
    hdr_source: str


def _is_valid_infolabel(label, value):
    """Echo guard: xbmc.getInfoLabel returns the literal label text when it
    cannot resolve a label, so value==label means "no data", not data."""
    return bool(value and value.strip() and value.lower() != label.lower())


def derive_stream_facts(player_id, raw_codec, raw_channels, raw_fps, raw_hdr,
                        raw_hdr_fallback, raw_gamut):
    """Pure derivation of a StreamProfile from raw single-shot readings.

    The HDR chain-of-evidence, echo guards, and HLG-via-gamut sniff are
    ported from legacy StreamInfo.gather_stream_info — including its
    asymmetry that the post-normalization echo check compares against the
    PRIMARY label even for the fallback value (the fallback returns ''
    rather than an echo when unresolved, so only the primary echo shape
    occurs in practice). The whitelists are gone (verbatim acceptance):
    audio keys as reported; an HDR string outside the classic five keys as
    reported too; fps is the exact parsed rate with no bucket check and no
    override collapse.
    """
    audio_format = keys.audio_segment(raw_codec)

    try:
        fps_value = float(raw_fps)
        # A rate must be finite and positive to count as detected: 'nan'/
        # 'inf' parse but would blow up fps_int()/key composition later, and
        # a reported 0 is the decoder's not-locked-yet placeholder — storing
        # under an <hdr>|0|<audio> key would strand the offset on a bucket
        # that never recurs (the classic int()+bucket gate refused both).
        if not math.isfinite(fps_value) or fps_value <= 0:
            fps_value = None
    except (ValueError, TypeError):
        fps_value = None

    if _is_valid_infolabel(INFOLABEL_HDR, raw_hdr):
        platform_hdr_full = True
        hdr_raw = raw_hdr
        hdr_source = 'primary'
    else:
        platform_hdr_full = False
        hdr_raw = raw_hdr_fallback
        hdr_source = 'fallback'

    hdr_type = keys.hdr_segment(hdr_raw)
    if hdr_type == formats.UNKNOWN:
        # Absent HDR reading: the chain-of-evidence default. (Echo shapes
        # never reach here: the primary branch is taken only after
        # _is_valid_infolabel screened its echo, and an unresolved fallback
        # reads '' — absence — rather than an echo.)
        hdr_type = 'sdr'
        hdr_source = 'default-sdr'

    gamut_valid = _is_valid_infolabel(INFOLABEL_GAMUT, raw_gamut)
    gamut_info = raw_gamut if gamut_valid else 'not available'
    if hdr_type == 'sdr' and gamut_valid and 'hlg' in raw_gamut.lower():
        hdr_type = 'hlg'
        hdr_source = 'gamut-hlg'

    profile = StreamProfile(
        hdr_type=hdr_type,
        audio_format=audio_format,
        video_fps=fps_value,
        player_id=player_id,
        audio_channels=raw_channels,
    )
    return StreamFacts(
        profile=profile,
        platform_hdr_full=platform_hdr_full,
        advanced_hlg=gamut_valid,
        gamut_info=gamut_info,
        hdr_source=hdr_source,
    )


class StreamDetector:
    """Probe/verify orchestration; sole writer of ``session.profile``."""

    PROBE_SPACING_SECONDS = 0.5
    # ~10s of discovery at 0.5s spacing: parity with legacy rpc_client's two
    # stacked retry loops (10x0.5s for the player id, then 10x0.5s for a
    # non-'none' codec).
    PROBE_BUDGET = 20
    VERIFY_WINDOW_SECONDS = 1.0

    _PROBE_KEY = 'aom.detector.probe'
    _VERIFY_KEY = 'aom.detector.verify'

    def __init__(self, dispatcher, session_tracker, gateway, settings_facade,
                 *, log_debug, log_warning, rng=random.random):
        self._dispatcher = dispatcher
        self._sessions = session_tracker
        self._gateway = gateway
        self._settings = settings_facade
        self._log = log_debug
        self._warn = log_warning
        self._rng = rng
        # Single live session at a time: plain fields, reset on start/stop.
        # Events stamped with a superseded session_id are dropped on receipt.
        self._discovering = False
        self._verify_seq = 0

        dispatcher.subscribe(events.PlaybackStarted, self._on_playback_started)
        dispatcher.subscribe(events.AvChanged, self._on_av_changed)
        dispatcher.subscribe(events.ProbeStream, self._on_probe)
        dispatcher.subscribe(events.VerifyStream, self._on_verify)
        dispatcher.subscribe(events.PlaybackStopped, self._on_playback_ended)
        dispatcher.subscribe(events.PlaybackEnded, self._on_playback_ended)

    # -- lifecycle (dispatcher thread) -----------------------------------------

    def _on_playback_started(self, _event):
        session = self._sessions.current
        if session is None:
            return  # tracker subscribes first; defensive only
        self._cancel_scheduled()
        self._discovering = True
        self._log(f"AOM_StreamDetector: session #{session.session_id} "
                  f"discovery started")
        self._dispatcher.post(
            events.ProbeStream(session_id=session.session_id, attempt=1))

    def _on_playback_ended(self, _event):
        self._cancel_scheduled()
        self._discovering = False

    def _cancel_scheduled(self):
        self._dispatcher.cancel(self._PROBE_KEY)
        self._dispatcher.cancel(self._VERIFY_KEY)

    # -- discovery: budgeted probe chain ---------------------------------------

    def _on_probe(self, event):
        if not self._sessions.is_alive(event.session_id):
            return  # superseded session: the scheduled probe is inert
        session = self._sessions.current
        facts = self._gather(event.session_id)
        if policies.is_complete(facts.profile):
            self._discovering = False
            self._log(f"AOM_StreamDetector: discovery complete on attempt "
                      f"{event.attempt}: {facts.profile}")
            self._adopt(session, facts.profile)
        elif event.attempt < self.PROBE_BUDGET:
            self._dispatcher.schedule(
                self._jittered_spacing(),
                events.ProbeStream(session_id=event.session_id,
                                   attempt=event.attempt + 1),
                key=self._PROBE_KEY)
        else:
            self._discovering = False
            self._warn(f"AOM_StreamDetector: giving up discovery after "
                       f"{event.attempt} attempts; last probe: {facts.profile}")

    # -- change detection --------------------------------------------------------

    def _on_av_changed(self, _event):
        session = self._sessions.current
        if session is None:
            self._log("AOM_StreamDetector: AV change with no session; ignoring")
            return
        if self._discovering:
            # The probe chain reads fresh facts on every attempt, so it will
            # observe whatever this change did — no extra work to schedule.
            self._log("AOM_StreamDetector: AV change during discovery; "
                      "probes will observe it")
            return
        facts = self._gather(session.session_id)
        if self._same_stream(facts.profile, session.profile):
            # Same offset-relevant stream: refresh incidental fields
            # (player_id/channels/raw fps) silently — no events, no state.
            session.profile = facts.profile
            self._log("AOM_StreamDetector: AV change with unchanged profile; "
                      "ignoring")
            return
        if policies.is_complete(facts.profile):
            self._log(f"AOM_StreamDetector: stream change detected: "
                      f"{session.profile} -> {facts.profile}")
            self._adopt(session, facts.profile)
        elif session.profile is None:
            # Discovery gave up earlier and the stream is still incomplete —
            # a change means it may be completing now; restart the budget
            # (legacy's in-call retry loops would have kept chasing here).
            self._discovering = True
            self._log("AOM_StreamDetector: AV change after exhausted "
                      "discovery; restarting probes")
            self._dispatcher.post(
                events.ProbeStream(session_id=session.session_id, attempt=1))
        else:
            # Had a complete profile, now incomplete: renegotiation in
            # flight. Regress to STABILIZING and let the verify loop
            # re-probe until the stream settles (recovery edge).
            session.mark_verifying()
            self._log("AOM_StreamDetector: profile lost mid-playback; "
                      "verifying until it settles")
            self._schedule_verify(session.session_id)

    # -- verification: whole-profile quiescence ---------------------------------

    def _on_verify(self, event):
        if not self._sessions.is_alive(event.session_id):
            return
        if event.seq != self._verify_seq:
            # Superseded verification. key-replace already supersedes the
            # pending timer; the seq guard documents intent and protects any
            # future path that lets a stale VerifyStream reach the queue.
            return
        session = self._sessions.current
        facts = self._gather(event.session_id)
        if self._same_stream(facts.profile, session.profile):
            session.profile = facts.profile   # silent incidental-field refresh
            session.mark_stable()
            announce = session.profile_changed_since_stabilized
            session.profile_changed_since_stabilized = False
            self._log(f"AOM_StreamDetector: profile held for "
                      f"{self.VERIFY_WINDOW_SECONDS}s; session "
                      f"#{event.session_id} stable "
                      f"(profile_changed={announce})")
            self._dispatcher.post(events.StreamStabilized(
                session_id=event.session_id, profile_changed=announce,
                initial=session.stabilized_count == 1))
        elif policies.is_complete(facts.profile):
            self._log(f"AOM_StreamDetector: profile changed during "
                      f"verification: {session.profile} -> {facts.profile}; "
                      f"re-verifying")
            self._adopt(session, facts.profile)
        else:
            # Profile went incomplete inside the window (codec blip):
            # keep watching. Session-bound: playback stop cancels the key.
            self._log("AOM_StreamDetector: profile incomplete during "
                      "verification; re-verifying")
            self._schedule_verify(event.session_id)

    # -- internals ----------------------------------------------------------------

    def _same_stream(self, profile, adopted):
        """Offset-relevant identity, at the granularity in force RIGHT NOW.

        The per-fps toggle is read at compare instant (never captured):
        with it off, an fps wiggle is an incidental-field refresh; with it
        on, the truncated rate is part of the identity exactly like the
        lookup key.
        """
        if adopted is None:
            return False
        per_fps = self._settings.per_fps_offsets_enabled()
        return (policies.stream_identity(profile, per_fps)
                == policies.stream_identity(adopted, per_fps))

    def _adopt(self, session, profile):
        """Write the session's profile and (re-)earn stability for it."""
        session.profile = profile
        session.profile_changed_since_stabilized = True
        if not session.mark_profile_built():
            session.mark_verifying()
        self._dispatcher.post(
            events.ProfileChanged(session_id=session.session_id))
        self._schedule_verify(session.session_id)

    def _schedule_verify(self, session_id):
        self._verify_seq += 1
        self._dispatcher.schedule(
            self.VERIFY_WINDOW_SECONDS,
            events.VerifyStream(session_id=session_id, seq=self._verify_seq),
            key=self._VERIFY_KEY)

    def _gather(self, session_id):
        """One single-shot detection pass; posts platform facts as it goes."""
        player_id = self._gateway.active_player_id()
        if player_id == -1:
            raw_codec, raw_channels = formats.UNKNOWN, formats.UNKNOWN
        else:
            raw_codec, raw_channels = self._gateway.audio_info(player_id)
        raw_fps = self._gateway.infolabel(INFOLABEL_FPS)
        raw_hdr = self._gateway.infolabel(INFOLABEL_HDR)
        raw_hdr_fallback = self._gateway.infolabel(INFOLABEL_HDR_FALLBACK)
        raw_gamut = self._gateway.infolabel(INFOLABEL_GAMUT)
        facts = derive_stream_facts(
            player_id=player_id,
            raw_codec=raw_codec,
            raw_channels=raw_channels,
            raw_fps=raw_fps,
            raw_hdr=raw_hdr,
            raw_hdr_fallback=raw_hdr_fallback,
            raw_gamut=raw_gamut,
        )
        # The raw gateway strings are logged VERBATIM: under the open
        # vocabulary they are the store's key material, and field logs are
        # how key fragmentation would ever be observed and diagnosed.
        self._log(f"AOM_StreamDetector: probed {facts.profile} "
                  f"(hdr_source={facts.hdr_source}, "
                  f"platform_hdr_full={facts.platform_hdr_full}, "
                  f"gamut={facts.gamut_info}, "
                  f"raw codec={raw_codec!r} hdr={raw_hdr!r}"
                  f"/{raw_hdr_fallback!r} fps={raw_fps!r})")
        self._dispatcher.post(events.StreamProbed(
            session_id=session_id,
            platform_hdr_full=facts.platform_hdr_full,
            advanced_hlg=facts.advanced_hlg))
        return facts

    def _jittered_spacing(self):
        # Legacy rpc_client retry jitter, verbatim: base*(0.8..1.2), floor 0.1s.
        return max(0.1, self.PROBE_SPACING_SECONDS * (0.8 + self._rng() * 0.4))
