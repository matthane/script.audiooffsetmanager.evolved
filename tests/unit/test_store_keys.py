"""Unit tests for aom.store.keys — the verbatim-acceptance key algebra.

These read as a contract: the VERBATIM PINS and FRACTIONAL-RATE PINS below are
the regression armor for the doctrine that Kodi's reported strings become keys
as presented, with only case-fold + trim (+ a `|` defense) applied.
"""

import pytest

from resources.lib.aom.domain import formats
from resources.lib.aom.store import keys


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
    assert keys.AUDIO_DISPLAY['truehd'] == 'TrueHD'
    assert keys.HDR_DISPLAY['hdr10+'] == 'HDR10+'


def test_describe_key_known():
    assert keys.describe_key('dolbyvision|all|truehd') == \
        'Dolby Vision | All rates | TrueHD'
    assert keys.describe_key('hdr10+|23|aac') == 'HDR10+ | 23 fps | AAC'


def test_describe_key_unknown_segments_render_verbatim():
    assert keys.describe_key('x-future-hdr|48|x-future-codec') == \
        'x-future-hdr | 48 fps | x-future-codec'


def test_truehd_atmos_display_name_is_field_observed():
    # E7 beta1 (Kodi 22 beta1/Windows): Atmos-flagged TrueHD reports
    # 'truehd_atmos' verbatim. The alias is DISPLAY-only — the key segment
    # stays verbatim, so stored data is untouched by the friendly name.
    assert keys.audio_segment('truehd_atmos') == 'truehd_atmos'
    assert keys.AUDIO_DISPLAY['truehd_atmos'] == 'TrueHD Atmos'
    assert keys.profile_summary('dolbyvision', 'truehd_atmos') == \
        'Dolby Vision | TrueHD Atmos'
