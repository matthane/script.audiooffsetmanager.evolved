"""Pure decision functions: offset gating, profile completeness, delay
parsing, and the seek quiet-window policy.

Pure Python: no Kodi imports, no I/O. Callers resolve settings/state and pass
explicit values; these functions only decide.
"""

from resources.lib.aome.domain import formats


def parse_delay_ms(delay_str):
    """Parse Kodi's localized `Player.AudioDelay` infolabel string to ms.

    Handles '-0.075 s', comma decimals ('-0,075 s'), a Unicode minus sign
    (U+2212, used by some CLDR locales), and narrow no-break spaces anywhere
    around the unit. Clamps to +/-10 s. Returns None on unparseable input.

    Parsing details that matter:

    - A narrow no-break space as the SOLE separator before the unit
      ('-0.075<U+202F>s', the CLDR unit-separator convention) parses:
      NNBSP is normalized to a regular space BEFORE the unit is stripped,
      never deleted (deleting it would leave '-0.075s' for float()).
    - The ms conversion rounds instead of truncating: float('-0.115') * 1000
      is -114.999..., and truncation would report -114 for a slider value
      of -115 ms.
    """
    try:
        normalized = (delay_str.replace('\u202f', ' ')  # narrow no-break space
                      .replace('\u2212', '-')  # Unicode minus sign (CLDR locales)
                      .replace(',', '.')
                      .strip())
        # Strip the trailing unit however it is separated: 's' never appears
        # inside a parseable number, so dropping one trailing 's' (plus any
        # remaining spaces) is unambiguous.
        if normalized.endswith('s'):
            normalized = normalized[:-1]
        normalized = normalized.replace(' ', '')
        delay_seconds = float(normalized)
        # Clamp to reasonable bounds (-10s to +10s) to avoid junk values
        delay_seconds = max(-10.0, min(delay_seconds, 10.0))
        return int(round(delay_seconds * 1000))
    except (ValueError, AttributeError):
        return None


def is_complete(profile):
    """True when the profile carries enough facts to key the store.

    Under the open vocabulary the HDR axis always resolves (the detector
    defaults an absent reading to 'sdr'), so completeness is: an audio
    format was reported, and the fps rate parsed. Requiring fps keeps
    discovery patient during startup renegotiation (probing continues
    until the fps axis settles — a stream whose rate is still unreadable
    is not ready to act on) and guarantees the per-fps write key is
    always composable for a complete profile.
    """
    if profile is None:
        return False
    return (profile.audio_format != formats.UNKNOWN
            and profile.hdr_type != formats.UNKNOWN
            and profile.video_fps is not None)


def stream_identity(profile, per_fps):
    """The "same stream" comparison key, at the granularity that matters.

    With the per-fps toggle OFF the fps axis does not exist for offsets, so
    it is excluded — a VFR rate wiggle between gathers must not read as a
    stream change. With the toggle ON the truncated rate is part of the
    identity, exactly like the lookup key.
    """
    if per_fps:
        return (profile.hdr_type, profile.fps_int(), profile.audio_format)
    return (profile.hdr_type, profile.audio_format)


def seek_decision(now, requested_at, last_activity, last_own_seek,
                  quiet_window, deadline, yield_to_activity=False):
    """The seek quiet-window policy: one rule for every seek-back request.

    Do not seek until there has been no seek activity — ours, another
    addon's, or the user's — for ``quiet_window`` seconds; defer otherwise;
    give up ``deadline`` seconds after the request. A request that another
    of our own seeks has already served (executed AT or AFTER the moment
    this request was made — same-instant counts as served, the safe side
    against a double rewind) is abandoned: its purpose — replaying the
    glitched seconds — is done.

    ``yield_to_activity`` is the stronger rule for triggers an external
    actor may mirror (the scheduler passes it for 'unpause'): ANY seek
    activity at or after ``requested_at`` yields the request instead of
    deferring it. Someone else moved the playhead after the trigger —
    another addon's own unpause seek-back, a repositioning, the user
    scrubbing — and replaying on top of their seek would double it.
    Activity strictly BEFORE the request never yields (the quiet window
    handles it): the rule is about the playhead having been touched since
    the trigger, not about recent busyness. Vendor-agnostic by
    construction: the caller's activity view is the generic aggregate
    (SeekOccurred, the vendor busy list, our own seeks), never a specific
    addon.

    Args (all timestamps monotonic; the caller resolves them):
        now: current time.
        requested_at: when this seek was requested.
        last_activity: most recent seek-like activity — any SeekOccurred,
            vendor busy signal, our own executed seek, or session start
            (session start counting as activity gives playback a settle
            window after start without a bespoke constant).
        last_own_seek: when WE last executed a seek this session, or None.
        quiet_window: required quiet seconds before seeking.
        deadline: max seconds after requested_at before giving up.
        yield_to_activity: apply the yield rule above (default off).

    Returns:
        'seek' | 'defer' | 'abandon' | 'yield'. Evaluated in a fixed
        order: served, then yielded, then deadline, then quietness.
        Deadline before quietness: a request that aged past the deadline
        is abandoned even if the window happens to be quiet now — a very
        late replay would itself be the disruption it was meant to
        repair. 'yield' is its own verdict (not folded into 'abandon')
        so the caller's log states WHY the replay stood down.
    """
    if last_own_seek is not None and last_own_seek >= requested_at:
        return 'abandon'
    if yield_to_activity and last_activity >= requested_at:
        return 'yield'
    if now - requested_at >= deadline:
        return 'abandon'
    if now - last_activity < quiet_window:
        return 'defer'
    return 'seek'


def should_apply(profile, apply_enabled):
    """Decide whether an offset may be applied for this profile.

    Applying requires the 'Apply audio offsets' toggle (it gates applying
    only, never learning) and a profile complete enough to key the store.
    An empty store needs no gate of its own: a lookup miss is already a
    no-op.

    Args:
        profile: StreamProfile or None.
        apply_enabled: bool — the apply toggle (caller resolves it).

    Returns:
        (allowed, reason) — reason is None when allowed, else one of
        'apply_off', 'no_profile', 'unknown_format'.
    """
    if not apply_enabled:
        return False, 'apply_off'
    if profile is None:
        return False, 'no_profile'
    if not is_complete(profile):
        return False, 'unknown_format'
    return True, None
