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
the UNKNOWN sentinel from aome.domain.formats, kept in lockstep so absence reads
the same everywhere.
"""

import math

from resources.lib.aome.domain.formats import UNKNOWN

# Segment joiner for the composite profile key.
SEPARATOR = '|'

# Strings that mean "Kodi reported nothing here" — one absence rule for
# every axis (a 'none' HDR and a 'none' audio are the same fact; splitting
# the rule per axis is how one absent value fragments into several keys).
_ABSENT = ('', 'none', UNKNOWN)

# The one proven, shipped HDR alias. Never grow this speculatively.
_HDR_ALIASES = {'hlghdr': 'hlg'}

# --- Display names (used by the management view) ----------------------------
# The fallback for an unrecognised segment is ALWAYS the raw segment itself,
# never a rejection — verbatim acceptance extends to how keys are shown.

HDR_DISPLAY = {
    'dolbyvision': 'Dolby Vision',
    'hdr10': 'HDR10',
    # Both spellings of HDR10+ display the same, and NEITHER aliases to
    # the other: 'hdr10plus' is what Kodi 22's native HDR10+ detection
    # reports and 'hdr10+' is the older spelling. A key
    # alias would rewrite future composition and strand offsets already
    # stored under the other spelling — display entries are free, key
    # rewrites are not.
    'hdr10+': 'HDR10+',
    'hdr10plus': 'HDR10+',
    'hlg': 'HLG',
    'sdr': 'SDR',
    UNKNOWN: 'Unknown',
}

# Commercial display names for the codec vocabulary Kodi can actually report.
# Sourced from Kodi's StreamUtils::GetCodecName (the profile-mapped names the
# JSON-RPC `currentaudiostream.codec` carries — the gateway strips the `pt-`
# passthrough prefix first) plus FFmpeg's canonical names on its fallback
# path. DISPLAY-only: these never participate in key matching, and an
# unlisted codec renders verbatim, so this table can grow or shrink without
# touching stored data.
AUDIO_DISPLAY = {
    # Dolby family, consistently branded ('truehd_atmos' and
    # 'eac3_ddp_atmos' are Kodi's verbatim profile reports).
    'truehd': 'Dolby TrueHD',
    'truehd_atmos': 'Dolby TrueHD Atmos',
    'eac3': 'Dolby Digital Plus',
    'eac3_ddp_atmos': 'Dolby Digital Plus Atmos',
    'ac3': 'Dolby Digital',
    # Kodi's demuxer knows AC-4 but StreamUtils has no special case for
    # it, so the reported name is FFmpeg's canonical 'ac4' (no Atmos
    # profile variant exists in FFmpeg — one spelling only).
    'ac4': 'Dolby AC-4',
    'mlp': 'MLP',
    # DTS family. Modern Kodi reports 'dts' for the base profile; 'dca' is
    # FFmpeg's canonical spelling kept for older report paths.
    'dts': 'DTS',
    'dca': 'DTS',
    'dts_es': 'DTS-ES',
    'dts_96_24': 'DTS 96/24',
    'dts_express': 'DTS Express',
    'dtshd_ma': 'DTS-HD MA',
    'dtshd_hra': 'DTS-HD HRA',
    'dtshd_ma_x': 'DTS:X',
    'dtshd_ma_x_imax': 'DTS:X IMAX',
    # AAC family (Kodi maps the MPEG profiles to their own names).
    'aac': 'AAC',
    'aac_lc': 'AAC-LC',
    'he_aac': 'HE-AAC',
    'he_aac_v2': 'HE-AAC v2',
    'aac_ssr': 'AAC SSR',
    'aac_ltp': 'AAC LTP',
    'aac_latm': 'AAC (LATM)',
    # Lossless / other.
    'flac': 'FLAC',
    'alac': 'ALAC',
    'opus': 'Opus',
    'vorbis': 'Vorbis',
    'mp3': 'MP3',
    'mp2': 'MP2',
    'wmav2': 'WMA',
    'wmapro': 'WMA Pro',
    'wmalossless': 'WMA Lossless',
    # PCM: FFmpeg names carry the sample layout; render the part a user
    # recognises. Rare layouts fall back verbatim like any other stranger.
    'pcm': 'PCM',
    'pcm_s16le': 'PCM 16-bit',
    'pcm_s24le': 'PCM 24-bit',
    'pcm_s32le': 'PCM 32-bit',
    'pcm_f32le': 'PCM 32-bit float',
    'pcm_bluray': 'PCM (Blu-ray)',
    'pcm_dvd': 'PCM (DVD)',
    UNKNOWN: 'Unknown Format',
}

# --- Toast short names (profile_summary only) --------------------------------
# The offset toast is a single narrow line in Kodi's toast label: the full
# commercial names ('Dolby Digital Plus Atmos') force Estuary's auto-scroll,
# so the toast renders standard AV shorthand instead. OVERLAYS: a segment
# missing here falls back to the full display table, then verbatim — the
# management view keeps the full names (it has the width, and inspection
# wants them). Only names with an established shorter form appear; nothing
# is invented ('TrueHD Atmos' is the honest floor, never 'THD').

HDR_DISPLAY_SHORT = {
    'dolbyvision': 'DV',
}

AUDIO_DISPLAY_SHORT = {
    'truehd': 'TrueHD',
    'truehd_atmos': 'TrueHD Atmos',
    'eac3': 'DD+',
    'eac3_ddp_atmos': 'DD+ Atmos',
    # 'AC3', not 'DD': it matches what Kodi's own OSD calls the codec,
    # where a bare 'DD' next to 'DD+' reads as a typo.
    'ac3': 'AC3',
    'ac4': 'AC-4',
    UNKNOWN: 'Unknown',
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
        # OverflowError: int(float('inf')) — non-finite rates are unparseable
        # too (the detector screens them, but this module is a public seam).
        return str(int(float(fps)))
    except (TypeError, ValueError, OverflowError):
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


def _display_fps(segment, video_fps=None, per_fps=False):
    if segment == 'all':
        # Under the per-fps toggle the 'all' entry is the FALLBACK for
        # rates without their own entry (lookup: exact -> all -> miss), so
        # 'All FPS' would misread as overriding the exact entries —
        # 'Other FPS' states the true semantics. With the toggle off,
        # 'all' is the only key consulted, so the axis carries no
        # information and the segment is OMITTED (None) — any dormant
        # exact-rate siblings keep their rate and are tagged inactive, so
        # the unlabelled row cannot be misread as one of them.
        # 'FPS' (not 'rates') to match the '<n> fps' unit on sibling rows.
        return 'Other FPS' if per_fps else None
    if isinstance(video_fps, (int, float)) and \
            not isinstance(video_fps, bool) and math.isfinite(video_fps):
        return "{0:g} fps".format(video_fps)
    return "{} fps".format(segment)


def describe_key(key, video_fps=None, per_fps=False):
    """Human-readable label, e.g. 'Dolby Vision | 23.976 fps | TrueHD'.

    HDR and audio segments use the display tables, falling back to the raw
    segment verbatim when unrecognised. The 'all' fps segment renders as
    'Other FPS' when ``per_fps`` says the toggle is on (it is the
    fallback below the exact-rate entries) and is OMITTED when off
    ('Dolby Vision | Dolby TrueHD') — with the toggle off 'all' is the
    only key consulted, so the axis says nothing. A numeric segment
    renders the EXACT reported rate from the entry's ``video_fps``
    metadata when the caller supplies a finite number ('23' is a key
    identity, not a rate a user recognises), degrading to the truncated
    segment ('<n> fps') when the metadata is absent or malformed
    (hand-edited file).
    """
    hdr, fps, audio = split_key(key)
    parts = [HDR_DISPLAY.get(hdr, hdr)]
    fps_name = _display_fps(fps, video_fps, per_fps)
    if fps_name is not None:
        parts.append(fps_name)
    parts.append(AUDIO_DISPLAY.get(audio, audio))
    return " | ".join(parts)


def describe_key_in_group(key, video_fps=None, per_fps=False):
    """In-group row label, e.g. 'Dolby TrueHD · 23.976 fps'.

    The management view's grouped drill-down lists one HDR type at a time,
    so its entry rows drop the redundant HDR name and lead with the codec
    (the stable-width part a user scans by). Same display vocabulary, fps
    semantics, and verbatim fallbacks as ``describe_key`` — including the
    omitted 'all' axis when the toggle is off, where the row is just the
    codec name; an unsplittable key raises ValueError exactly like
    ``describe_key`` does — callers show those keys as themselves (the
    view's 'Other' bucket).
    """
    _hdr, fps, audio = split_key(key)
    audio_name = AUDIO_DISPLAY.get(audio, audio)
    fps_name = _display_fps(fps, video_fps, per_fps)
    if fps_name is None:
        return audio_name
    return "{} · {}".format(audio_name, fps_name)


def sort_key(key):
    """Deterministic display ordering: HDR type, then codec, then rate.

    Groups the management view's rows the way a user scans them — all of
    one HDR mode together, codecs alphabetical within it, and each codec's
    'all' entry before its per-fps entries in NUMERIC rate order
    (string-sorting '119' before '23' is exactly the bug this avoids).
    Display names (case-folded) drive the alpha ordering so the on-screen
    grouping matches the sort. Total over hand-edited files: an
    unsplittable key sorts by its raw text; a non-numeric fps segment
    sorts after the numeric rates; the raw key is the final tie-break.
    """
    try:
        hdr, fps, audio = split_key(key)
    except ValueError:
        return (key.lower(), '', (0, 0), key)
    if fps == 'all':
        fps_rank = (0, 0)
    else:
        try:
            fps_rank = (1, int(fps))
        except ValueError:
            fps_rank = (2, 0)
    return (
        HDR_DISPLAY.get(hdr, hdr).lower(),
        AUDIO_DISPLAY.get(audio, audio).lower(),
        fps_rank,
        key,
    )


def profile_summary(hdr_segment_value, audio_segment_value, video_fps=None):
    """Toast/log summary straight from profile facts (no key needed).

    E.g. 'DV | 23.976 fps | TrueHD Atmos'; without a rate, 'DV | TrueHD'.
    Uses the SHORT display overlays (this is the one single-line surface;
    the management view keeps the full names), falling back to the full
    table and then verbatim — verbatim acceptance extends to display. The
    exact reported rate is shown (it is the management-view metadata too).
    """
    parts = [HDR_DISPLAY_SHORT.get(
        hdr_segment_value,
        HDR_DISPLAY.get(hdr_segment_value, hdr_segment_value))]
    if video_fps is not None:
        parts.append("{0:g} fps".format(video_fps))
    parts.append(AUDIO_DISPLAY_SHORT.get(
        audio_segment_value,
        AUDIO_DISPLAY.get(audio_segment_value, audio_segment_value)))
    return " | ".join(parts)
