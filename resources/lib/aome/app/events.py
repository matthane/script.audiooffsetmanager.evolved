"""Typed events dispatched on the aome dispatcher.

Events are frozen dataclasses dispatched by type (subscribe registers against
the class). Payloads are explicit fields — never positional *args.

Every event has a live producer; SeekChapter and SpeedChanged are posted by
the player bridge but currently have no consumer — kept deliberately so the
bridge covers Kodi's full playback-callback surface (DESIGN marks them
reserved; note that chapter jumps also fire onPlayBackSeek, so the seek
quiet window already sees them via SeekOccurred). Pure Python: no Kodi
imports.
"""

from dataclasses import dataclass


# --- Player/monitor events (posted by kodi.player_bridge / monitor_bridge) --

@dataclass(frozen=True)
class PlaybackStarted:
    """Kodi onAVStarted: audio and video are rendering."""


@dataclass(frozen=True)
class AvChanged:
    """Kodi onAVChange: raw, noisy; stability is judged downstream."""


@dataclass(frozen=True)
class PlaybackStopped:
    """Kodi onPlayBackStopped: user stopped playback."""


@dataclass(frozen=True)
class PlaybackEnded:
    """Kodi onPlayBackEnded: playback reached the end."""


@dataclass(frozen=True)
class Paused:
    """Kodi onPlayBackPaused."""


@dataclass(frozen=True)
class Resumed:
    """Kodi onPlayBackResumed."""


@dataclass(frozen=True)
class SeekOccurred:
    """Kodi onPlayBackSeek — any seek, from any source (feeds quiet window)."""
    time_ms: int
    offset_ms: int


@dataclass(frozen=True)
class SeekChapter:
    """Kodi onPlayBackSeekChapter."""
    chapter: int


@dataclass(frozen=True)
class SpeedChanged:
    """Kodi onPlayBackSpeedChanged."""
    speed: int


@dataclass(frozen=True)
class SettingsChanged:
    """Kodi Monitor.onSettingsChanged: refresh cached flags; never write here."""


# --- Detection events (posted/consumed from the StreamDetector phase on) ----

@dataclass(frozen=True)
class ProbeStream:
    """Self-scheduled stream probe attempt for a session."""
    session_id: int
    attempt: int


@dataclass(frozen=True)
class VerifyStream:
    """Scheduled whole-profile stability verification (key-replaced)."""
    session_id: int
    seq: int


@dataclass(frozen=True)
class StreamProbed:
    """A detection pass observed the platform (log-only observability).

    Posted on EVERY gather — probes, AV-change re-probes, and verifications.
    The fields are facts about what the platform reported; nothing stores
    them anymore (the PlatformRecorder dissolved with the capability flags —
    capability gating is emergent from the store, P3). They stay on the
    event because field logs showing WHICH detection path fired are the
    debugging lifeline.
    """
    session_id: int
    platform_hdr_full: bool
    advanced_hlg: bool


@dataclass(frozen=True)
class StreamStabilized:
    """The session's profile held for the verification window.

    ``profile_changed`` is False for a pure re-confirmation (a blip that
    reverted with no adoption in between): the state machine re-earned
    STABLE, but no stream change is being announced. The seek scheduler
    skips the 'adjust' replay for those — legacy's duplicate-codec filter
    never fired an event for a reverting blip either. The default is True
    (announce) so hand-posted events keep the announcing behavior.

    ``initial`` is True on the session's FIRST stabilization — startup
    settling, not a mid-play change. The detector stamps it from
    ``session.stabilized_count`` (owned by the state machine's only edge
    into STABLE), and the seek scheduler skips the 'adjust' replay for it —
    what used to be a consumer-side latch on the session. The default is
    False (the mid-play case) so hand-posted change events act like changes.
    """
    session_id: int
    profile_changed: bool = True
    initial: bool = False


@dataclass(frozen=True)
class ProfileChanged:
    """The session's profile was created or replaced."""
    session_id: int


# --- Offset/adjustment events -----------------------------------------------

@dataclass(frozen=True)
class OffsetApplied:
    """An offset was applied via JSON-RPC (provisional until STABLE)."""
    session_id: int
    profile: object  # StreamProfile
    ms: int
    provisional: bool


@dataclass(frozen=True)
class DelayReset:
    """A zero-reset RPC landed (baseline or deleted-profile).

    Posted by the applier's reset paths on a SUCCESSFUL reset only — the
    already-0, preserve, and failed-RPC branches move nothing and post
    nothing (mutation-time observation invalidation is therefore handled
    SYNCHRONOUSLY in the StoreMutationHandler, not here). The watcher
    consumes it to drop any in-flight observation: a reset is an
    automatic delay change exactly like an apply, so the supersede
    corollary applies. No OffsetApplied fires for resets — that event
    drives the notifier's apply toast, which resets never raise; note
    the diverged-baseline reset is not otherwise silent (it posts
    UnsavedOffsetDiscarded for the "Offset not saved" toast, D3
    amendment), while the deleted-profile reset is fully silent (D3
    second amendment).
    """
    session_id: int


@dataclass(frozen=True)
class UnsavedOffsetDiscarded:
    """The zero-reset discarded a manual adjustment that was never stored.

    Posted by the applier's miss path (D3 amendment, E7): when an
    unlearned profile resets the delay to baseline and the value in force
    DIVERGED from the last AOM apply, the difference was the user's hand
    — remember-adjustments off, or a stream change inside the quiescence
    window. The Notifier raises the "Offset not saved" toast so the user
    knows why their adjustment vanished; a reset of pure AOM residue posts
    nothing (silent by design). ``ms`` is the discarded value, for the
    toast/log only — it is never written anywhere.
    """
    session_id: int
    profile: object  # StreamProfile
    ms: int


@dataclass(frozen=True)
class UserOffsetSettled:
    """A manual audio-offset adjustment held through quiescence.

    The USER-ACTION fact, posted by the AdjustmentWatcher at the settle
    instant for every quiesced foreign value — before, and regardless
    of, whether the learn loop stores it (``UserOffsetSaved`` is the
    STORAGE fact). Consumers reacting to the user's adjustment itself —
    the seek scheduler's 'change' replay — subscribe here, so they keep
    working with learning off, an incomplete profile, or an unwritable
    store (beta9 field pass: the replay follows the user's hand, not the
    learn loop). ``ms`` is the settled value, for logging only.
    """
    session_id: int
    ms: int


@dataclass(frozen=True)
class UserOffsetSaved:
    """The adjustment watcher stored a user's manual offset change.

    Session-stamped, and the profile/ms ride on the event as captured AT
    STORE TIME on the dispatcher thread: consumers (notification, 'change'
    seek-back) act on exactly what was stored, and an in-place reopen
    between post and dispatch makes the event inert instead of targeting
    the new session — the legacy USER_ADJUSTMENT bus wire carried no
    payload and no stamp, leaving both races open (P5 review finding).

    ``key`` is the store key the value landed under, resolved by the D4
    rule at store instant — consumers log/announce exactly what was stored.
    """
    session_id: int
    profile: object  # StreamProfile
    ms: int
    key: str = None


# --- Seek scheduling events --------------------------------------------------

@dataclass(frozen=True)
class ExecuteSeek:
    """Self-scheduled seek execution attempt (re-validated at fire time).

    ``requested_at`` (monotonic) rides on the event — the ProbeStream
    pattern — so the deadline is measured from the request that scheduled
    this attempt chain with no side bookkeeping; a re-request key-replaces
    the chain with a fresh requested_at (deadline restart).
    """
    session_id: int
    reason: str
    requested_at: float


# --- Notifier events ----------------------------------------------------------

@dataclass(frozen=True)
class RaiseToast:
    """Self-scheduled toast release delayed past a fading predecessor.

    The fade-guard section of ``aome.app.notifier``'s module docstring is the
    authoritative account of the mechanics and the supersede semantics.

    Deliberately NOT session-stamped: the payload describes a fact that
    already happened and stays true (and worth announcing) even if the
    session ends inside the deferral. The surface is pre-rendered at request
    time — kind-specific rendering happens before scheduling, so every toast
    kind can ride this one event.
    """
    message: str
    title: object       # str, or None for the gui's addon-name default
    duration_ms: int
    dedupe_key: object  # Notifier dedupe identity; None for notices
    enabled: object     # bound per-kind gate accessor re-checked at fire
                        # time (live setting; D10), or None = ungated notice


# --- Watcher events -----------------------------------------------------------

@dataclass(frozen=True)
class WatchTick:
    """Recurring adjustment-watcher poll tick for a session."""
    session_id: int


# --- Store lifecycle ----------------------------------------------------------

@dataclass(frozen=True)
class StoreCorrupted:
    """The offsets file was quarantined to .bad at load (one-shot).

    Posted by the runtime after construction when the store's corruption
    flag was set; the Notifier owns the user-facing notice (E3-review
    ledger item — the composition root no longer raises GUI toasts).
    """


# --- Store mutation channel (script process -> service, D5) ------------------

@dataclass(frozen=True)
class StoreMutationRequested:
    """A cross-process store mutation request received over NotifyAll.

    Posted by the monitor bridge VERBATIM from the (untrusted) payload —
    fields may be None or wrong-typed; the StoreMutationHandler owns
    validation and the op whitelist (delete/clear/import ONLY, P6: the
    channel structurally cannot carry a value write — there is no value
    field, and import reads values only from the staged backup file).
    ``request_id`` is echoed back on the ack so the script process can
    match replies.
    """
    op: object
    key: object = None
    request_id: object = None


@dataclass(frozen=True)
class StoreMutated:
    """A whitelisted mutation changed the LIVE (in-memory) store.

    Posted by the StoreMutationHandler after a delete that removed an
    entry or a clear with entries — INCLUDING their persist-failed
    variants, because OffsetStore keeps the in-memory removal and reset
    markers when only the disk write fails, and the live session resolves
    against memory (the ack separately reports the durability truth). A
    missing-key delete, an empty clear, and refused ops changed nothing
    and post nothing. The applier consumes it exactly like
    ``SettingsChanged``: a mutation is a resolve moment for the live
    session (E7, user call 2026-07-16 — deleting the playing profile's
    offset takes effect immediately, not at the next playback), and it
    changes no profile, so the same foreign-delay preservation applies.
    ``op``/``key`` ride along for the debug trail only.
    """
    op: str
    key: object = None
