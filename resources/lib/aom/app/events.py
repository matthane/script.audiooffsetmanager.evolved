"""Typed events dispatched on the aom dispatcher.

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
class DeletedProfileReset:
    """A reset marker fired: a deleted profile's delay was forced to 0.

    Posted by the applier's miss path (D3 second amendment, E7): the user
    deleted this profile in the management view expecting 0 on its next
    playback, and Kodi's per-file memory was still holding a nonzero
    delay — the forced 0 is the deletion completing, so the Notifier
    confirms it with the "reset to 0" toast. A marker consumed against a
    delay already at 0 posts nothing (nothing visible happened). ``ms``
    is the value that was wiped, for the toast/log only.
    """
    session_id: int
    profile: object  # StreamProfile
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
    validation and the op whitelist (delete/clear ONLY, P6: the channel
    structurally cannot carry a value write — there is no value field).
    ``request_id`` is echoed back on the ack so the script process can
    match replies.
    """
    op: object
    key: object = None
    request_id: object = None
