"""Unit tests for aome.domain.profile — verbatim facts, identity, describe.

Store keys
are composed by ``aome.store`` at lookup/write instant. What the profile owns:
immutability, the truncated fps axis, the offset-relevant identity
tuple, and the greppable ``describe()`` log form.
"""

import dataclasses

import pytest

from resources.lib.aome.domain.profile import StreamProfile


def make_profile(hdr_type='dolbyvision', audio_format='truehd',
                 video_fps=23.976, player_id=1, audio_channels=8):
    return StreamProfile(hdr_type=hdr_type, audio_format=audio_format,
                         video_fps=video_fps, player_id=player_id,
                         audio_channels=audio_channels)


def test_profile_is_frozen():
    profile = make_profile()
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.hdr_type = 'sdr'


def test_profile_has_no_setting_id():
    # The settings-matrix key died with the matrix; the store composes keys
    # at lookup/write instant. Nothing may resurrect a captured-key API.
    assert not hasattr(make_profile(), 'setting_id')


# --- fps truncation (the key-axis guarantee) ----------------------------------

@pytest.mark.parametrize("video_fps, expected", [
    (23.976, 23), (24.0, 24),        # NTSC pair stays distinct
    (29.97, 29), (30.0, 30),
    (59.94, 59), (60.0, 60),
    (119.88, 119), (120.0, 120),
    (48.0, 48),                       # open-ended: no bucket whitelist
    (25, 25),                         # int input passes through
])
def test_fps_int_truncates(video_fps, expected):
    assert make_profile(video_fps=video_fps).fps_int() == expected


def test_fps_int_none_when_undetected():
    assert make_profile(video_fps=None).fps_int() is None


@pytest.mark.parametrize("audio_channels, expected", [
    (8, 8), (6, 6), (2, 2), (1, 1),
    ('6', 6),                         # numeric string passes through
    (22, 22),                         # open-ended: no layout whitelist
])
def test_channels_int_normalizes(audio_channels, expected):
    assert make_profile(audio_channels=audio_channels).channels_int() \
        == expected


@pytest.mark.parametrize("unusable", [
    'unknown', '', None, 0, -2, True, False,
    float('nan'), float('inf'), float('-inf'),
])
def test_channels_int_none_when_unusable(unusable):
    # Mirrors the store's channel_segment rule: an unusable count means the
    # stream has no channel axis (bool guarded — True must never read as 1).
    assert make_profile(audio_channels=unusable).channels_int() is None


# --- identity ------------------------------------------------------------------

def test_identity_is_the_offset_relevant_tuple():
    assert make_profile().identity() == ('dolbyvision', 23, 'truehd')


def test_identity_excludes_incidental_fields():
    a = make_profile(player_id=1, audio_channels=8)
    b = make_profile(player_id=2, audio_channels=6)
    assert a.identity() == b.identity()


def test_identity_carries_verbatim_open_vocabulary():
    profile = make_profile(hdr_type='hdr10+', audio_format='x-future-codec')
    assert profile.identity() == ('hdr10+', 23, 'x-future-codec')


# --- describe (field-log form) ---------------------------------------------------

def test_describe_is_greppable_key_shape():
    assert make_profile().describe() == 'dolbyvision|23|truehd|8'


def test_describe_marks_missing_fps():
    assert make_profile(video_fps=None).describe() == 'dolbyvision|?|truehd|8'


def test_describe_marks_unusable_channels():
    assert make_profile(audio_channels='unknown').describe() == \
        'dolbyvision|23|truehd|?'
