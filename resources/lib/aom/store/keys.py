"""Profile-key algebra for the sparse offset store.

DESIGN DOCTRINE — VERBATIM ACCEPTANCE.

Evolved stores audio offsets against stream-profile keys derived from what
Kodi REPORTS, exactly as presented. There is NO whitelist, NO substring
matching, and NO alias table gating which codecs or HDR types are "known." A
codec or HDR string this code has never encountered becomes a working key with
zero code changes — that is the whole point. Normalization is deliberately
minimal: case-fold + trim, plus a defensive `|` substitution (see below).

Exactly ONE historical alias survives, and only because it is proven, shipped
detection behaviour rather than speculation: `hlghdr` -> `hlg`. Do NOT add
speculative aliases. Real fragmentation observed in the field is the only
justification for any future addition here.

KEY SHAPE: ``<hdr>|<fps>|<audio>`` — e.g. ``dolbyvision|23|truehd`` or
``hdr10+|all|aac``. The `|` joiner is why `normalize_segment` maps any stray
`|` inside a raw string to `_`: the separator must never appear inside a
segment, so `split_key` can always recover exactly three parts.

Absence handling (in `audio_segment`) is NOT a whitelist: an empty string,
'none', or 'unknown' all mean "Kodi reported nothing," so they collapse to the
UNKNOWN sentinel. Every other audio string passes through verbatim.

The empty-HDR default is intentionally split across layers: `hdr_segment`
maps a blank HDR to 'unknown', NOT to 'sdr'. Choosing 'sdr' for an absent HDR
flag is a chain-of-evidence decision (fallback flags, colour-gamut echoes)
that belongs to the stream detector, which has that context — deliberately not
this pure key module.

FPS uses integer TRUNCATION (`str(int(float(fps)))`). Truncation — not
rounding — is what keeps NTSC fractional rates on their own keys: 23.976 -> 23
stays distinct from 24.0 -> 24, 29.97 -> 29 from 30.0 -> 30, and so on. When
the per-FPS override is off, the fps segment is the literal 'all'.

Pure Python: stdlib only, no xbmc* imports. The lone cross-module reference is
the UNKNOWN sentinel from aom.domain.formats, kept in lockstep so absence reads
the same everywhere.
"""

from resources.lib.aom.domain.formats import UNKNOWN

# Segment joiner for the composite profile key.
SEPARATOR = '|'

# Strings that mean "Kodi reported nothing here" — one absence rule for
# every axis (a 'none' HDR and a 'none' audio are the same fact; splitting
# the rule per axis is how one absent value fragments into several keys).
_ABSENT = ('', 'none', UNKNOWN)

# The one proven, shipped HDR alias. Never grow this speculatively.
_HDR_ALIASES = {'hlghdr': 'hlg'}

# --- Display names (used by the future management view) ---------------------
# The fallback for an unrecognised segment is ALWAYS the raw segment itself,
# never a rejection — verbatim acceptance extends to how keys are shown.

HDR_DISPLAY = {
    'dolbyvision': 'Dolby Vision',
    'hdr10': 'HDR10',
    # Verbatim acceptance means the '+' survives into the key: 'hdr10+' is
    # the only spelling the store ever produces ('hdr10plus' was the
    # settings-id-era rewrite and would be a speculative alias here).
    'hdr10+': 'HDR10+',
    'hlg': 'HLG',
    'sdr': 'SDR',
    UNKNOWN: 'Unknown',
}

AUDIO_DISPLAY = {
    'truehd': 'TrueHD',
    'eac3': 'E-AC-3',
    'ac3': 'AC-3',
    'dtshd_ma': 'DTS-HD MA',
    'dtshd_hra': 'DTS-HD HRA',
    'dca': 'DTS',
    'pcm': 'PCM',
    'aac': 'AAC',
    'flac': 'FLAC',
    'opus': 'Opus',
    UNKNOWN: 'Unknown Format',
}


def normalize_segment(raw):
    """Case-fold + trim a raw segment, then neutralise any stray separator.

    The `|` substitution is purely defensive: `|` is the key joiner and is
    never expected inside a real codec/HDR string, but replacing it with `_`
    guarantees `split_key` can always recover exactly three parts. May return
    '' (an empty raw string normalises to '').
    """
    return str(raw).strip().lower().replace(SEPARATOR, '_')


def audio_segment(raw):
    """Normalise an audio string; collapse reported-absence to UNKNOWN.

    '', 'none', and 'unknown' all mean Kodi reported no audio format and map to
    the UNKNOWN sentinel. Every other value passes through verbatim — no
    whitelist, no substring collapse (e.g. 'pcm_s24le' stays 'pcm_s24le').
    """
    segment = normalize_segment(raw)
    if segment in _ABSENT:
        return UNKNOWN
    return segment


def hdr_segment(raw):
    """Normalise an HDR string; apply the lone proven alias, absence to UNKNOWN.

    The absence rule is the SAME one audio uses ('', 'none', 'unknown' →
    UNKNOWN): an HDR axis Kodi reported nothing for must collapse to one
    sentinel, not fragment across 'none'/'unknown' keys. Choosing 'sdr' for
    absent HDR is the stream detector's chain-of-evidence job, not this
    module's. 'hlghdr' -> 'hlg' is the only alias (shipped detection
    behaviour).
    """
    segment = normalize_segment(raw)
    if segment in _ABSENT:
        return UNKNOWN
    return _HDR_ALIASES.get(segment, segment)


def fps_segment(fps, per_fps):
    """The fps segment: 'all' when per-FPS is off, else the truncated integer.

    When `per_fps` is falsy the value is ignored and the literal 'all' is
    returned. Otherwise the rate is truncated to an integer via
    ``int(float(fps))`` so fractional NTSC rates stay distinct from their
    integer siblings. Unparseable input (None, '', 'abc') raises ValueError —
    callers must gate on profile completeness before composing a per-FPS key.
    """
    if not per_fps:
        return 'all'
    if isinstance(fps, bool):
        # bool is an int subclass: True would silently become segment '1'.
        raise ValueError("fps_segment: unparseable fps value {!r}".format(fps))
    try:
        return str(int(float(fps)))
    except (TypeError, ValueError):
        raise ValueError("fps_segment: unparseable fps value {!r}".format(fps))


def profile_key(hdr_raw, fps, audio_raw, *, per_fps):
    """Compose the full ``<hdr>|<fps>|<audio>`` profile key."""
    return SEPARATOR.join((
        hdr_segment(hdr_raw),
        fps_segment(fps, per_fps),
        audio_segment(audio_raw),
    ))


def all_key(hdr_raw, audio_raw):
    """The fallback-level key ``<hdr>|all|<audio>``.

    Delegates to ``profile_key`` with the toggle off (which forces the 'all'
    segment) so the key shape has exactly one composition point.
    """
    return profile_key(hdr_raw, None, audio_raw, per_fps=False)


def split_key(key):
    """Invert a profile key into ``(hdr, fps, audio)``; ValueError if not 3 parts."""
    parts = key.split(SEPARATOR)
    if len(parts) != 3:
        raise ValueError("split_key: expected 3 segments, got {!r}".format(key))
    return parts[0], parts[1], parts[2]


def _display_fps(segment):
    if segment == 'all':
        return 'All rates'
    return "{} fps".format(segment)


def describe_key(key):
    """Human-readable label, e.g. 'Dolby Vision | 23 fps | TrueHD'.

    HDR and audio segments use the display tables, falling back to the raw
    segment verbatim when unrecognised. The fps segment renders as 'All rates'
    for 'all', otherwise '<n> fps'.
    """
    hdr, fps, audio = split_key(key)
    hdr_name = HDR_DISPLAY.get(hdr, hdr)
    audio_name = AUDIO_DISPLAY.get(audio, audio)
    return "{} | {} | {}".format(hdr_name, _display_fps(fps), audio_name)


def profile_summary(hdr_segment_value, audio_segment_value, video_fps=None):
    """Toast/log summary straight from profile facts (no key needed).

    E.g. 'Dolby Vision | 23.976 fps | TrueHD'; without a rate,
    'Dolby Vision | TrueHD'. The exact reported rate is shown (it is the
    management-view metadata too); unrecognised segments render verbatim —
    verbatim acceptance extends to display.
    """
    parts = [HDR_DISPLAY.get(hdr_segment_value, hdr_segment_value)]
    if video_fps is not None:
        parts.append("{0:g} fps".format(video_fps))
    parts.append(AUDIO_DISPLAY.get(audio_segment_value, audio_segment_value))
    return " | ".join(parts)
