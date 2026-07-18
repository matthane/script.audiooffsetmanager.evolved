"""Unit tests for aome.store.keys — the verbatim-acceptance key algebra.

These read as a contract: the VERBATIM PINS and FRACTIONAL-RATE PINS below are
the regression armor for the doctrine that Kodi's reported strings become keys
as presented, with only case-fold + trim (+ a `|` defense) applied.
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


def test_hdr_verbatim_passthrough():
    # The '+' SURVIVES: it was settings-id scaffolding that ever stripped it.
    assert keys.hdr_segment('HDR10+') == 'hdr10+'
    # Inner space survives; only the ends are trimmed.
    assert keys.hdr_segment('Dolby Vision') == 'dolby vision'


# --- The one proven alias ---------------------------------------------------

def test_hlghdr_alias_is_the_only_one():
    assert keys.hdr_segment('hlghdr') == 'hlg'
    assert keys.hdr_segment('HLGHDR') == 'hlg'
    assert keys.hdr_segment('hlg') == 'hlg'


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
    hdr, fps, audio = keys.split_key(key)
    assert (hdr, fps, audio) == ('sdr', '24', 'weird_name')


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
        'dolbyvision|23|truehd'


def test_all_key_composition():
    assert keys.all_key('DolbyVision', 'TrueHD') == 'dolbyvision|all|truehd'


def test_split_key_inverts_composition():
    assert keys.split_key('dolbyvision|23|truehd') == \
        ('dolbyvision', '23', 'truehd')
    assert keys.split_key('dolbyvision|all|truehd') == \
        ('dolbyvision', 'all', 'truehd')


def test_split_key_requires_three_parts():
    with pytest.raises(ValueError):
        keys.split_key('dolbyvision|truehd')
    with pytest.raises(ValueError):
        keys.split_key('dolbyvision|23|truehd|extra')


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
    assert keys.HDR_DISPLAY['hdr10+'] == 'HDR10+'


def test_hdr10plus_display_name_is_field_observed():
    # E7 beta9 (Kodi 22 beta1/Windows): Kodi's native HDR10+ detection
    # reports 'hdr10plus', which rendered verbatim until this display
    # entry. DISPLAY-only, deliberately NOT an alias: the segment stays
    # verbatim in keys ('hdr10plus' composes and matches as itself), so
    # offsets already stored under either spelling keep resolving.
    assert keys.hdr_segment('hdr10plus') == 'hdr10plus'
    assert keys.HDR_DISPLAY['hdr10plus'] == 'HDR10+'
    assert keys.describe_key('hdr10plus|all|truehd') == \
        'HDR10+ | Dolby TrueHD'
    assert keys.profile_summary('hdr10plus', 'truehd') == \
        'HDR10+ | TrueHD'


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
    assert keys.describe_key('dolbyvision|all|truehd') == \
        'Dolby Vision | Dolby TrueHD'
    assert keys.describe_key('hdr10+|23|aac') == 'HDR10+ | 23 fps | AAC'


def test_describe_key_unknown_segments_render_verbatim():
    assert keys.describe_key('x-future-hdr|48|x-future-codec') == \
        'x-future-hdr | 48 fps | x-future-codec'


def test_describe_key_shows_exact_rate_from_video_fps_metadata():
    # E7 beta4 field feedback: '23 fps' is key identity, not a rate a user
    # recognises — the entry's video_fps metadata renders the EXACT rate.
    assert keys.describe_key('dolbyvision|23|eac3', video_fps=23.976) == \
        'Dolby Vision | 23.976 fps | Dolby Digital Plus'
    assert keys.describe_key('hdr10|59|ac3', video_fps=59.94) == \
        'HDR10 | 59.94 fps | Dolby Digital'
    # Whole rates render clean (no trailing '.0').
    assert keys.describe_key('hdr10|24|ac3', video_fps=24.0) == \
        'HDR10 | 24 fps | Dolby Digital'


def test_describe_key_all_segment_is_toggle_aware():
    # per_fps ON: the 'all' entry is the fallback BELOW exact-rate entries
    # (exact -> all -> miss), so 'All FPS' would misread as an override —
    # it renders 'Other FPS'. OFF: 'all' is the only key consulted, so
    # the fps axis carries no information and is omitted (the default).
    assert keys.describe_key('dolbyvision|all|truehd', per_fps=True) == \
        'Dolby Vision | Other FPS | Dolby TrueHD'
    assert keys.describe_key('dolbyvision|all|truehd', per_fps=False) == \
        'Dolby Vision | Dolby TrueHD'
    # A numeric segment is unaffected by the toggle.
    assert keys.describe_key('hdr10|23|ac3', video_fps=23.976,
                             per_fps=True) == 'HDR10 | 23.976 fps | Dolby Digital'


def test_describe_key_all_key_ignores_video_fps_metadata():
    # 'all' is the identity: the entry's rate is just the last store
    # instant's, not what the key matches.
    assert keys.describe_key('dolbyvision|all|truehd', video_fps=23.976) == \
        'Dolby Vision | Dolby TrueHD'
    assert keys.describe_key('dolbyvision|all|truehd', video_fps=23.976,
                             per_fps=True) == \
        'Dolby Vision | Other FPS | Dolby TrueHD'


def test_describe_key_degrades_to_segment_without_usable_metadata():
    # Absent or malformed (hand-edited file) metadata falls back to the
    # truncated segment rather than crashing or rendering garbage.
    assert keys.describe_key('hdr10+|23|aac') == 'HDR10+ | 23 fps | AAC'
    for bad in ('23.976', True, float('nan'), float('inf'), None):
        assert keys.describe_key('hdr10+|23|aac', video_fps=bad) == \
            'HDR10+ | 23 fps | AAC'


def test_describe_key_in_group_drops_hdr_and_leads_with_codec():
    # The drill-down row: one HDR group is open, so its name is redundant —
    # the codec leads (the stable-width part) and the rate follows. With
    # the toggle off the 'all' axis is omitted too: just the codec.
    assert keys.describe_key_in_group('dolbyvision|all|truehd') == \
        'Dolby TrueHD'
    assert keys.describe_key_in_group(
        'dolbyvision|23|truehd', video_fps=23.976, per_fps=True) == \
        'Dolby TrueHD · 23.976 fps'


def test_describe_key_in_group_matches_describe_key_semantics():
    # Same fps display rules as describe_key: toggle-aware 'all', exact
    # rate from metadata, segment degradation without it — one vocabulary.
    assert keys.describe_key_in_group('dolbyvision|all|truehd',
                                      per_fps=True) == \
        'Dolby TrueHD · Other FPS'
    assert keys.describe_key_in_group('hdr10|59|ac3') == \
        'Dolby Digital · 59 fps'
    # Verbatim fallback for an unlisted codec, like every display surface.
    assert keys.describe_key_in_group('sdr|all|x-future-codec') == \
        'x-future-codec'


def test_describe_key_in_group_raises_on_unsplittable_key():
    # Same contract as describe_key: the caller owns the verbatim fallback
    # (the view's 'Other' bucket shows the raw key as itself).
    with pytest.raises(ValueError):
        keys.describe_key_in_group('not-a-key')


def test_sort_key_groups_hdr_then_codec_then_numeric_rate():
    ordered = sorted([
        'hdr10|all|ac3',
        'dolbyvision|24|truehd',
        'dolbyvision|119|eac3',
        'dolbyvision|23|eac3',
        'dolbyvision|all|eac3',
    ], key=keys.sort_key)
    assert ordered == [
        'dolbyvision|all|eac3',      # 'all' before per-fps rates
        'dolbyvision|23|eac3',       # numeric: 23 < 119 (not lexicographic)
        'dolbyvision|119|eac3',
        'dolbyvision|24|truehd',     # codec groups within the HDR mode
        'hdr10|all|ac3',
    ]


def test_sort_key_is_total_over_hand_edited_keys():
    # Unsplittable keys and non-numeric fps segments must sort somewhere
    # deterministic without raising (verbatim-acceptance doctrine: a
    # scribbled file renders, never crashes).
    scribbles = ['not-a-key', 'hdr10|abc|ac3', 'dolbyvision|23|eac3']
    ordered = sorted(scribbles * 2, key=keys.sort_key)
    assert ordered == sorted(scribbles * 2, key=keys.sort_key)  # stable
    assert len(ordered) == 6


def test_truehd_atmos_display_name_is_field_observed():
    # E7 beta1 (Kodi 22 beta1/Windows): Atmos-flagged TrueHD reports
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
    assert keys.describe_key('dolbyvision|all|eac3_ddp_atmos') == \
        'Dolby Vision | Dolby Digital Plus Atmos'
    # A segment with no short form falls back to the FULL display name...
    assert keys.profile_summary('hdr10', 'dtshd_ma') == 'HDR10 | DTS-HD MA'
    # ...and an unlisted segment still renders verbatim (acceptance
    # doctrine extends to display).
    assert keys.profile_summary('x-future-hdr', 'x-future-codec') == \
        'x-future-hdr | x-future-codec'
    # Absence reads 'Unknown' on the short surface, not 'Unknown Format'.
    assert keys.profile_summary('sdr', keys.UNKNOWN) == 'SDR | Unknown'
