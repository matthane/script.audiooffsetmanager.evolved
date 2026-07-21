"""Unit tests for aome.store.keys — the verbatim-acceptance key algebra.

These read as a contract: the VERBATIM PINS and FRACTIONAL-RATE PINS below are
the regression armor for the doctrine that Kodi's reported strings become keys
as presented, with only case-fold + trim (+ a `|` defense) applied — plus, on
the HDR axis only, the cross-build canonicalization (whitespace strip and the
field-observed alias set) that keeps one format on one key across Kodi builds.
"""

import pytest

from resources.lib.aome.domain import formats
from resources.lib.aome.store import keys


# --- Verbatim pins: strings pass through with only case-fold + trim ---------

def test_audio_verbatim_passthrough():
    assert keys.audio_segment('aac') == 'aac'
    assert keys.audio_segment(' Opus ') == 'opus'
    assert keys.audio_segment('FLAC') == 'flac'
    assert keys.audio_segment('DTS') == 'dts'
    # NO collapse to a canonical 'pcm' — the reported string is the key.
    assert keys.audio_segment('PCM_S24LE') == 'pcm_s24le'
    assert keys.audio_segment('x-future-codec') == 'x-future-codec'
    # INNER whitespace survives on the audio axis (only the ends trim):
    # the HDR axis's whitespace strip is evidence-gated and must never
    # silently spread here.
    assert keys.audio_segment('x future codec') == 'x future codec'
    assert keys.normalize_segment(' a b ') == 'a b'


def test_hdr_verbatim_passthrough():
    # An unheard-of HDR string becomes a working key with no code changes,
    # and no character other than whitespace/`|` is ever rewritten (the
    # '+' survives in a stranger).
    assert keys.hdr_segment('x-future-hdr+') == 'x-future-hdr+'
    assert keys.hdr_segment(' HDR10 ') == 'hdr10'


# --- Cross-build canonicalization: the observed fragmentation set -----------

def test_hdr_aliases_unify_the_observed_cross_build_spellings():
    # Kodi 21's primary HDR infolabel reports 'HDR10+' where Kodi 22's
    # native detection reports 'hdr10plus': one canonical key keeps a
    # learned offset matching on both builds (and portable via backup).
    assert keys.hdr_segment('HDR10+') == 'hdr10plus'
    assert keys.hdr_segment('hdr10plus') == 'hdr10plus'
    assert keys.hdr_segment('hlghdr') == 'hlg'
    assert keys.hdr_segment('HLGHDR') == 'hlg'
    assert keys.hdr_segment('hlg') == 'hlg'


def test_hdr_segment_strips_internal_whitespace():
    # Kodi 21 reports 'Dolby Vision' (spaced title case) where Kodi 22
    # reports 'dolbyvision': the whitespace strip lands every spacing
    # variant on the canonical spelling without per-variant aliases.
    assert keys.hdr_segment('Dolby Vision') == 'dolbyvision'
    assert keys.hdr_segment('dolby  vision') == 'dolbyvision'
    # The strip feeds the alias table: a spaced report of an aliased
    # spelling still lands on the canonical key.
    assert keys.hdr_segment('HLG HDR') == 'hlg'


def test_cross_build_spellings_compose_identical_keys():
    assert keys.profile_key('Dolby Vision', 23.976, 'TrueHD_Atmos',
                            per_fps=True) == \
        keys.profile_key('dolbyvision', 23.976, 'truehd_atmos', per_fps=True)
    assert keys.all_key('HDR10+', 'ac3') == keys.all_key('hdr10plus', 'ac3')


# --- Absence handling -------------------------------------------------------

def test_audio_absence_collapses_to_unknown():
    assert keys.audio_segment('') == formats.UNKNOWN
    assert keys.audio_segment('none') == formats.UNKNOWN
    assert keys.audio_segment('unknown') == formats.UNKNOWN
    assert keys.audio_segment('none') == 'unknown'


def test_hdr_blank_defaults_to_unknown_not_sdr():
    assert keys.hdr_segment('') == formats.UNKNOWN
    assert keys.hdr_segment('') == 'unknown'


def test_hdr_absence_rule_matches_audio():
    # ONE absence rule for every axis: an HDR reported as 'none'/'unknown'
    # is the same fact as a blank one and must not fragment into its own
    # keys ('none|all|truehd' etc.).
    assert keys.hdr_segment('none') == formats.UNKNOWN
    assert keys.hdr_segment('unknown') == formats.UNKNOWN
    assert keys.hdr_segment('NONE') == formats.UNKNOWN


# --- `|` separator defense --------------------------------------------------

def test_bool_fps_is_rejected_not_key_1():
    # bool is an int subclass: True must raise, never become segment '1'.
    with pytest.raises(ValueError):
        keys.fps_segment(True, True)
    with pytest.raises(ValueError):
        keys.fps_segment(False, True)


def test_pipe_in_segment_is_neutralised():
    assert keys.audio_segment('weird|name') == 'weird_name'
    key = keys.profile_key('sdr', 24, 'weird|name', per_fps=True)
    assert 'weird_name' in key
    hdr, fps, audio, ch = keys.split_key(key)
    assert (hdr, fps, audio, ch) == ('sdr', '24', 'weird_name', 'all')


# --- Fractional-rate pins: truncation keeps NTSC siblings distinct ----------

@pytest.mark.parametrize('frac, integer, frac_key, int_key', [
    (23.976, 24.0, '23', '24'),
    (29.97, 30.0, '29', '30'),
    (59.94, 60.0, '59', '60'),
    (119.88, 120.0, '119', '120'),
])
def test_fractional_rates_stay_distinct(frac, integer, frac_key, int_key):
    assert keys.fps_segment(frac, per_fps=True) == frac_key
    assert keys.fps_segment(integer, per_fps=True) == int_key
    assert frac_key != int_key


def test_open_vocabulary_rates():
    assert keys.fps_segment(48, per_fps=True) == '48'
    assert keys.fps_segment(25, per_fps=True) == '25'
    assert keys.fps_segment('25.000', per_fps=True) == '25'


# --- per_fps toggle ---------------------------------------------------------

def test_per_fps_off_is_all_and_ignores_value():
    assert keys.fps_segment(23.976, per_fps=False) == 'all'
    assert keys.fps_segment(None, per_fps=False) == 'all'
    assert keys.fps_segment('abc', per_fps=False) == 'all'


def test_per_fps_on_with_unparseable_raises():
    with pytest.raises(ValueError):
        keys.fps_segment(None, per_fps=True)
    with pytest.raises(ValueError):
        keys.fps_segment('', per_fps=True)
    with pytest.raises(ValueError):
        keys.fps_segment('abc', per_fps=True)


# --- Composition and inversion ----------------------------------------------

def test_profile_key_composition():
    assert keys.profile_key('DolbyVision', 23.976, 'TrueHD', per_fps=True) == \
        'dolbyvision|23|truehd|all'


def test_all_key_composition():
    assert keys.all_key('DolbyVision', 'TrueHD') == 'dolbyvision|all|truehd|all'


def test_split_key_inverts_composition():
    assert keys.split_key('dolbyvision|23|truehd|all') == \
        ('dolbyvision', '23', 'truehd', 'all')
    assert keys.split_key('dolbyvision|all|truehd|6') == \
        ('dolbyvision', 'all', 'truehd', '6')


def test_split_key_requires_four_parts():
    # Callers see post-canonicalization keys, so the legacy 3-segment
    # shape never reaches split_key — it raises like any other misfit.
    with pytest.raises(ValueError):
        keys.split_key('dolbyvision|truehd')
    with pytest.raises(ValueError):
        keys.split_key('dolbyvision|23|truehd')
    with pytest.raises(ValueError):
        keys.split_key('dolbyvision|23|truehd|all|extra')


# --- Idempotence property ---------------------------------------------------

def test_segment_functions_are_idempotent():
    raw_strings = [
        'HDR10+', ' truehd ', 'x|y', 'HLGHDR', 'aac', 'PCM_S24LE',
        'Dolby Vision', 'FLAC', ' Opus ', 'x-future-codec', 'DTS',
        'hlg', 'none', 'sdr', 'weird|name',
    ]
    for raw in raw_strings:
        once = keys.audio_segment(raw)
        assert keys.audio_segment(once) == once
        h_once = keys.hdr_segment(raw)
        assert keys.hdr_segment(h_once) == h_once
        n_once = keys.normalize_segment(raw)
        assert keys.normalize_segment(n_once) == n_once


# --- Display helpers --------------------------------------------------------

def test_display_known_names():
    assert keys.AUDIO_DISPLAY['truehd'] == 'Dolby TrueHD'
    assert keys.HDR_DISPLAY['hdr10plus'] == 'HDR10+'


def test_hdr10plus_display_needs_only_the_canonical_entry():
    # The store canonicalizes keys at its boundary, so display code only
    # ever sees the canonical 'hdr10plus' — no alias-source entries in
    # the display tables.
    assert keys.hdr_segment('hdr10plus') == 'hdr10plus'
    assert 'hdr10+' not in keys.HDR_DISPLAY
    assert keys.describe_key('hdr10plus|all|truehd|all') == \
        'HDR10+ | Dolby TrueHD'
    assert keys.profile_summary('hdr10plus', 'truehd') == \
        'HDR10+ | TrueHD'


def test_canonical_key_rewrites_legacy_spellings():
    # The store-boundary re-keying: entries and reset markers written by
    # an older codec land on the keys live composition produces today
    # (a schema-1 key additionally gains its channel axis).
    assert keys.canonical_key('hdr10+|all|truehd|all') == \
        'hdr10plus|all|truehd|all'
    assert keys.canonical_key('dolby vision|23|truehd_atmos|8') == \
        'dolbyvision|23|truehd_atmos|8'
    assert keys.canonical_key('hlghdr|all|aac') == 'hlg|all|aac|all'


def test_canonical_key_expands_schema1_keys_with_the_channel_axis():
    # THE v1→v2 migration: a three-segment key gains a trailing 'all',
    # losslessly and idempotently, at the same boundary every stored key
    # already crosses. No other segment is touched.
    assert keys.canonical_key('dolbyvision|23|truehd') == \
        'dolbyvision|23|truehd|all'
    assert keys.canonical_key('sdr|all|aac') == 'sdr|all|aac|all'
    expanded = keys.canonical_key('hdr10+|all|truehd')
    assert expanded == 'hdr10plus|all|truehd|all'
    assert keys.canonical_key(expanded) == expanded
    # Legacy scribbles expand too (the fps segment still passes through
    # unrewritten).
    assert keys.canonical_key('sdr|weird-fps|aac') == 'sdr|weird-fps|aac|all'


def test_canonical_key_is_idempotent_and_open_vocabulary():
    # A canonical key round-trips unchanged, and so does a format this
    # code has never seen — future Kodi formats need no code change.
    assert keys.canonical_key('hdr10plus|all|truehd|all') == \
        'hdr10plus|all|truehd|all'
    assert keys.canonical_key('x-future-hdr|48|x-future-codec|12') == \
        'x-future-hdr|48|x-future-codec|12'
    spaced = keys.canonical_key('some new format|all|aac|all')
    assert keys.canonical_key(spaced) == spaced


def test_canonical_key_leaves_scribbles_alone():
    # Keys that split to neither shape (hand-edited files) pass through
    # verbatim; the fps and channel segments are already composed and are
    # never rewritten.
    assert keys.canonical_key('not-a-key') == 'not-a-key'
    assert keys.canonical_key('dv|23|truehd|all|extra') == \
        'dv|23|truehd|all|extra'
    assert keys.canonical_key('sdr|weird-fps|aac|weird-ch') == \
        'sdr|weird-fps|aac|weird-ch'


@pytest.mark.parametrize('segment, commercial', [
    # Dolby family: commercial names, not the E-AC-3/AC-3 spec spellings.
    ('ac3', 'Dolby Digital'),
    ('eac3', 'Dolby Digital Plus'),
    ('eac3_ddp_atmos', 'Dolby Digital Plus Atmos'),
    ('ac4', 'Dolby AC-4'),
    ('truehd', 'Dolby TrueHD'),
    ('truehd_atmos', 'Dolby TrueHD Atmos'),
    # DTS family: Kodi's StreamUtils profile names, incl. modern 'dts'
    # alongside FFmpeg's legacy 'dca' spelling of the same fact.
    ('dts', 'DTS'),
    ('dca', 'DTS'),
    ('dts_es', 'DTS-ES'),
    ('dts_96_24', 'DTS 96/24'),
    ('dts_express', 'DTS Express'),
    ('dtshd_ma', 'DTS-HD MA'),
    ('dtshd_hra', 'DTS-HD HRA'),
    ('dtshd_ma_x', 'DTS:X'),
    ('dtshd_ma_x_imax', 'DTS:X IMAX'),
    # AAC profile names.
    ('aac_lc', 'AAC-LC'),
    ('he_aac', 'HE-AAC'),
    ('he_aac_v2', 'HE-AAC v2'),
    ('aac_latm', 'AAC (LATM)'),
    # Lossless / PCM.
    ('alac', 'ALAC'),
    ('vorbis', 'Vorbis'),
    ('pcm_s24le', 'PCM 24-bit'),
    ('pcm_bluray', 'PCM (Blu-ray)'),
])
def test_commercial_names_cover_kodis_codec_vocabulary(segment, commercial):
    # The table mirrors Kodi's StreamUtils::GetCodecName vocabulary (what
    # JSON-RPC currentaudiostream.codec reports, `pt-` stripped) plus
    # FFmpeg's canonical names on its fallback path. DISPLAY-only: the key
    # segment stays verbatim, so stored data is untouched by friendly names.
    assert keys.audio_segment(segment) == segment
    assert keys.AUDIO_DISPLAY[segment] == commercial


def test_describe_key_known():
    assert keys.describe_key('dolbyvision|all|truehd|all') == \
        'Dolby Vision | Dolby TrueHD'
    assert keys.describe_key('hdr10plus|23|aac|all') == \
        'HDR10+ | 23 fps | AAC'


def test_describe_key_unknown_segments_render_verbatim():
    assert keys.describe_key('x-future-hdr|48|x-future-codec|all') == \
        'x-future-hdr | 48 fps | x-future-codec'


def test_describe_key_shows_exact_rate_from_video_fps_metadata():
    # '23 fps' is key identity, not a rate a user
    # recognises — the entry's video_fps metadata renders the EXACT rate.
    assert keys.describe_key('dolbyvision|23|eac3|all', video_fps=23.976) == \
        'Dolby Vision | 23.976 fps | Dolby Digital Plus'
    assert keys.describe_key('hdr10|59|ac3|all', video_fps=59.94) == \
        'HDR10 | 59.94 fps | Dolby Digital'
    # Whole rates render clean (no trailing '.0').
    assert keys.describe_key('hdr10|24|ac3|all', video_fps=24.0) == \
        'HDR10 | 24 fps | Dolby Digital'


def test_describe_key_all_segment_is_toggle_aware():
    # per_fps ON: the 'all' entry is dormant (the lookup consults only
    # the fps-specific key), so 'All FPS' states its true scope — every
    # rate, in the other mode — and the view's dormancy tag carries the
    # not-in-effect part. OFF: 'all' is the only key consulted, so the
    # fps axis carries no information and is omitted (the default).
    assert keys.describe_key('dolbyvision|all|truehd|all', per_fps=True) == \
        'Dolby Vision | All FPS | Dolby TrueHD'
    assert keys.describe_key('dolbyvision|all|truehd|all', per_fps=False) == \
        'Dolby Vision | Dolby TrueHD'
    # A numeric segment is unaffected by the toggle.
    assert keys.describe_key('hdr10|23|ac3|all', video_fps=23.976,
                             per_fps=True) == \
        'HDR10 | 23.976 fps | Dolby Digital'


def test_describe_key_all_key_ignores_video_fps_metadata():
    # 'all' is the identity: the entry's rate is just the last store
    # instant's, not what the key matches.
    assert keys.describe_key('dolbyvision|all|truehd|all',
                             video_fps=23.976) == \
        'Dolby Vision | Dolby TrueHD'
    assert keys.describe_key('dolbyvision|all|truehd|all', video_fps=23.976,
                             per_fps=True) == \
        'Dolby Vision | All FPS | Dolby TrueHD'


def test_describe_key_degrades_to_segment_without_usable_metadata():
    # Absent or malformed (hand-edited file) metadata falls back to the
    # truncated segment rather than crashing or rendering garbage.
    assert keys.describe_key('hdr10plus|23|aac|all') == 'HDR10+ | 23 fps | AAC'
    for bad in ('23.976', True, float('nan'), float('inf'), None):
        assert keys.describe_key('hdr10plus|23|aac|all', video_fps=bad) == \
            'HDR10+ | 23 fps | AAC'


def test_describe_key_channel_segment_is_toggle_aware():
    # Same rule as the fps axis: a specific count always renders (its
    # layout name, or '<n> ch' verbatim for an unmapped count); the 'all'
    # segment renders its scope only in the mode where it is dormant.
    assert keys.describe_key('dolbyvision|all|truehd|6') == \
        'Dolby Vision | Dolby TrueHD | 5.1'
    assert keys.describe_key('dolbyvision|all|truehd|8',
                             distinct_channels=True) == \
        'Dolby Vision | Dolby TrueHD | 7.1'
    assert keys.describe_key('sdr|all|aac|4') == 'SDR | AAC | 4 ch'
    assert keys.describe_key('dolbyvision|all|truehd|all',
                             distinct_channels=True) == \
        'Dolby Vision | Dolby TrueHD | All channels'
    # Both axes compose.
    assert keys.describe_key('dolbyvision|23|truehd|2', video_fps=23.976,
                             per_fps=True) == \
        'Dolby Vision | 23.976 fps | Dolby TrueHD | 2.0'


def test_describe_key_in_group_drops_hdr_and_leads_with_codec():
    # The drill-down row: one HDR group is open, so its name is redundant —
    # the codec leads (the stable-width part) and the rate follows. With
    # the toggle off the 'all' axis is omitted too: just the codec.
    assert keys.describe_key_in_group('dolbyvision|all|truehd|all') == \
        'Dolby TrueHD'
    assert keys.describe_key_in_group(
        'dolbyvision|23|truehd|all', video_fps=23.976, per_fps=True) == \
        'Dolby TrueHD · 23.976 fps'


def test_describe_key_in_group_matches_describe_key_semantics():
    # Same axis display rules as describe_key: toggle-aware 'all', exact
    # rate from metadata, segment degradation without it — one vocabulary.
    assert keys.describe_key_in_group('dolbyvision|all|truehd|all',
                                      per_fps=True) == \
        'Dolby TrueHD · All FPS'
    assert keys.describe_key_in_group('hdr10|59|ac3|all') == \
        'Dolby Digital · 59 fps'
    assert keys.describe_key_in_group('hdr10|59|ac3|6', video_fps=59.94,
                                      per_fps=True) == \
        'Dolby Digital · 59.94 fps · 5.1'
    assert keys.describe_key_in_group('dolbyvision|all|truehd|all',
                                      distinct_channels=True) == \
        'Dolby TrueHD · All channels'
    # Verbatim fallback for an unlisted codec, like every display surface.
    assert keys.describe_key_in_group('sdr|all|x-future-codec|all') == \
        'x-future-codec'


def test_describe_key_in_group_raises_on_unsplittable_key():
    # Same contract as describe_key: the caller owns the verbatim fallback
    # (the view's 'Other' bucket shows the raw key as itself).
    with pytest.raises(ValueError):
        keys.describe_key_in_group('not-a-key')


def test_sort_key_groups_hdr_then_codec_then_numeric_rate():
    ordered = sorted([
        'hdr10|all|ac3|all',
        'dolbyvision|24|truehd|all',
        'dolbyvision|119|eac3|all',
        'dolbyvision|23|eac3|all',
        'dolbyvision|all|eac3|all',
    ], key=keys.sort_key)
    assert ordered == [
        'dolbyvision|all|eac3|all',   # 'all' before per-fps rates
        'dolbyvision|23|eac3|all',    # numeric: 23 < 119 (not lexicographic)
        'dolbyvision|119|eac3|all',
        'dolbyvision|24|truehd|all',  # codec groups within the HDR mode
        'hdr10|all|ac3|all',
    ]


def test_sort_key_channel_axis_breaks_ties_like_fps():
    # Within one codec+rate: 'all' first, then counts ascending
    # numerically ('12' after '6', not before).
    ordered = sorted([
        'sdr|all|aac|12',
        'sdr|all|aac|6',
        'sdr|all|aac|all',
        'sdr|all|aac|2',
    ], key=keys.sort_key)
    assert ordered == [
        'sdr|all|aac|all',
        'sdr|all|aac|2',
        'sdr|all|aac|6',
        'sdr|all|aac|12',
    ]


def test_sort_key_is_total_over_hand_edited_keys():
    # Unsplittable keys and non-numeric axis segments must sort somewhere
    # deterministic without raising (verbatim-acceptance doctrine: a
    # scribbled file renders, never crashes).
    scribbles = ['not-a-key', 'hdr10|abc|ac3|all', 'dolbyvision|23|eac3|xyz']
    ordered = sorted(scribbles * 2, key=keys.sort_key)
    assert ordered == sorted(scribbles * 2, key=keys.sort_key)  # stable
    assert len(ordered) == 6


def test_truehd_atmos_display_name_is_field_observed():
    # Field-observed on Kodi 22: Atmos-flagged TrueHD reports
    # 'truehd_atmos' verbatim. The alias is DISPLAY-only — the key segment
    # stays verbatim, so stored data is untouched by the friendly name.
    assert keys.audio_segment('truehd_atmos') == 'truehd_atmos'
    assert keys.AUDIO_DISPLAY['truehd_atmos'] == 'Dolby TrueHD Atmos'
    assert keys.profile_summary('dolbyvision', 'truehd_atmos') == \
        'DV | TrueHD Atmos'


def test_profile_summary_uses_short_names_with_full_table_fallback():
    # The toast line is the one single-line surface: Dolby names render as
    # standard AV shorthand there. The overlay is DISPLAY-only and toast-
    # only — describe_key (the management view) keeps the full names.
    assert keys.profile_summary('dolbyvision', 'eac3_ddp_atmos', 23.976) == \
        'DV | 23.976 fps | DD+ Atmos'
    assert keys.profile_summary('dolbyvision', 'ac3') == 'DV | AC3'
    assert keys.describe_key('dolbyvision|all|eac3_ddp_atmos|all') == \
        'Dolby Vision | Dolby Digital Plus Atmos'
    # A segment with no short form falls back to the FULL display name...
    assert keys.profile_summary('hdr10', 'dtshd_ma') == 'HDR10 | DTS-HD MA'
    # ...and an unlisted segment still renders verbatim (acceptance
    # doctrine extends to display).
    assert keys.profile_summary('x-future-hdr', 'x-future-codec') == \
        'x-future-hdr | x-future-codec'
    # Absence reads 'Unknown' on the short surface, not 'Unknown Format'.
    assert keys.profile_summary('sdr', keys.UNKNOWN) == 'SDR | Unknown'


# --- Spatial-mode pins: the audio axis's granularity flag --------------------

def test_audio_segment_default_is_verbatim():
    # The default serves the mode-independent callers (canonical_key,
    # display): a variant collapses only when a caller passes the mode.
    assert keys.audio_segment('truehd_atmos') == 'truehd_atmos'
    assert keys.audio_segment('dtshd_ma_x') == 'dtshd_ma_x'


def test_audio_segment_collapses_variants_with_distinct_off():
    assert keys.audio_segment(' TrueHD_Atmos ', False) == 'truehd'
    assert keys.audio_segment('EAC3_DDP_Atmos', False) == 'eac3'
    assert keys.audio_segment('dtshd_ma_x', False) == 'dtshd_ma'
    assert keys.audio_segment('dtshd_ma_x_imax', False) == 'dtshd_ma'


def test_audio_segment_collapse_leaves_non_variants_alone():
    for raw in ('truehd', 'eac3', 'dtshd_ma', 'dtshd_hra', 'x-future-codec'):
        assert keys.audio_segment(raw, False) == raw


def test_audio_absence_is_unknown_in_both_spatial_modes():
    for raw in ('', 'none', 'unknown'):
        assert keys.audio_segment(raw, False) == formats.UNKNOWN
        assert keys.audio_segment(raw, True) == formats.UNKNOWN


def test_profile_key_composes_the_spatial_mode():
    assert keys.profile_key('dolbyvision', 23.976, 'truehd_atmos',
                            per_fps=True, distinct_spatial=False) \
        == 'dolbyvision|23|truehd|all'
    assert keys.all_key('dolbyvision', 'truehd_atmos',
                        distinct_spatial=False) == 'dolbyvision|all|truehd|all'


def test_canonical_key_never_spatial_collapses():
    # Boundary canonicalization is mode-independent: collapsing here would
    # destructively rewrite stored variant keys while the toggle is off.
    assert keys.canonical_key('dolbyvision|all|truehd_atmos|all') \
        == 'dolbyvision|all|truehd_atmos|all'
    assert keys.canonical_key('sdr|23|dtshd_ma_x_imax|all') \
        == 'sdr|23|dtshd_ma_x_imax|all'


# --- Channel-mode pins: the channel axis's granularity flag ------------------

def test_channel_segment_off_is_all_and_ignores_value():
    assert keys.channel_segment(6, False) == 'all'
    assert keys.channel_segment(None, False) == 'all'
    assert keys.channel_segment('unknown', False) == 'all'
    # The default serves mode-independent callers, like the other axes.
    assert keys.channel_segment(6) == 'all'


def test_channel_segment_on_is_the_verbatim_count():
    assert keys.channel_segment(6, True) == '6'
    assert keys.channel_segment(8, True) == '8'
    assert keys.channel_segment(2, True) == '2'
    # Open vocabulary: any positive count keys, mapped display or not.
    assert keys.channel_segment(22, True) == '22'
    assert keys.channel_segment('6', True) == '6'


def test_channel_segment_degrades_to_all_never_raises():
    # Unlike fps there is no completeness gate upstream, so an unusable
    # count degrades to 'all' — the intended key for a channel-less
    # stream, identical in lookup and write. Non-finite floats are junk
    # like any other (OverflowError from int(inf) must not escape).
    for bad in (None, 'unknown', '', 'abc', 0, -2, True, False,
                float('nan'), float('inf'), float('-inf')):
        assert keys.channel_segment(bad, True) == 'all'


def test_profile_key_composes_the_channel_mode():
    assert keys.profile_key('dolbyvision', 23.976, 'truehd_atmos',
                            per_fps=True, channels=8,
                            distinct_channels=True) \
        == 'dolbyvision|23|truehd_atmos|8'
    assert keys.all_key('dolbyvision', 'truehd', channels=6,
                        distinct_channels=True) == 'dolbyvision|all|truehd|6'
    # Off: the count is ignored, like fps off.
    assert keys.all_key('dolbyvision', 'truehd', channels=6,
                        distinct_channels=False) == \
        'dolbyvision|all|truehd|all'


def test_canonical_key_never_collapses_the_channel_axis():
    # Same rule as spatial: a mode-dependent rewrite here would
    # destructively rewrite stored count keys while the toggle is off.
    assert keys.canonical_key('dolbyvision|all|truehd|6') == \
        'dolbyvision|all|truehd|6'
    assert keys.canonical_key('sdr|23|aac|2') == 'sdr|23|aac|2'


def test_profile_summary_channel_axis_is_offset_relevance_gated():
    # The caller passes channels only when the count is offset-relevant
    # (distinct-channels on), mirroring how fps rides the summary.
    assert keys.profile_summary('dolbyvision', 'truehd_atmos', 23.976, 8) == \
        'DV | 23.976 fps | TrueHD Atmos | 7.1'
    assert keys.profile_summary('hdr10', 'aac', None, 4) == \
        'HDR10 | AAC | 4 ch'
    assert keys.profile_summary('hdr10', 'aac', None, None) == 'HDR10 | AAC'
    # An unusable count renders nothing rather than 'All channels'.
    assert keys.profile_summary('hdr10', 'aac', None, 'unknown') == \
        'HDR10 | AAC'
