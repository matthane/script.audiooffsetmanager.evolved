"""Unit tests for aome.domain.policies — parsing, completeness, gating.

The full parse_delay_ms locale/clamping matrix lives in
tests/unit/test_delay_parsing.py; this suite covers the gating and
completeness policies plus a parsing sample.
"""

import pytest

from resources.lib.aome.domain import policies
from resources.lib.aome.domain.profile import StreamProfile

NNBSP = " "


def make_profile(hdr_type="hdr10", audio_format="truehd", video_fps=23.976,
                 audio_channels=6):
    return StreamProfile(
        hdr_type=hdr_type,
        audio_format=audio_format,
        video_fps=video_fps,
        player_id=1,
        audio_channels=audio_channels,
    )


# --- parse_delay_ms ----------------------------------------------------------

@pytest.mark.parametrize("delay_str, expected", [
    ("-0.075 s", -75),
    ("0.075 s", 75),
    ("-0,075 s", -75),              # comma decimal
    ("-0.075" + NNBSP + " s", -75),  # NNBSP before regular-space unit
    ("-15.5 s", -10000),             # clamps low
    ("20 s", 10000),                 # clamps high
])
def test_parse_delay_ms_valid(delay_str, expected):
    assert policies.parse_delay_ms(delay_str) == expected


@pytest.mark.parametrize("delay_str", ["abc", "", None, "s"])
def test_parse_delay_ms_junk_returns_none(delay_str):
    assert policies.parse_delay_ms(delay_str) is None


def test_parse_delay_ms_nnbsp_sole_separator_parses():
    # NNBSP directly against the unit — the CLDR
    # unit-separator convention — parses like any other separator.
    assert policies.parse_delay_ms("-0.075" + NNBSP + "s") == -75


def test_parse_delay_ms_unicode_minus_sign():
    # Some CLDR locales render negatives with U+2212.
    assert policies.parse_delay_ms("−0.075 s") == -75


def test_parse_delay_ms_rounds_instead_of_truncating():
    # Float('-0.115') * 1000 is -114.999...; int() used to
    # truncate a -115 ms slider value to -114.
    assert policies.parse_delay_ms("-0.115 s") == -115
    assert policies.parse_delay_ms("0.115 s") == 115


# --- is_complete -------------------------------------------------------------

def test_is_complete_none_profile():
    assert policies.is_complete(None) is False


def test_is_complete_full_profile():
    assert policies.is_complete(make_profile()) is True


@pytest.mark.parametrize("kwargs", [
    {"hdr_type": "unknown"},
    {"audio_format": "unknown"},
    {"video_fps": None},
])
def test_is_complete_missing_axis(kwargs):
    assert policies.is_complete(make_profile(**kwargs)) is False


def test_is_complete_open_vocabulary_counts_as_detected():
    # Verbatim acceptance: a format the code never heard of is a DETECTED
    # format — completeness gates on absence, not on a whitelist.
    assert policies.is_complete(
        make_profile(hdr_type="hdr10+", audio_format="x-future-codec")) is True


# --- stream_identity ---------------------------------------------------------

def test_identity_ignores_fps_when_toggle_off():
    # A VFR rate wiggle must not read as a stream change with per-fps off.
    a = make_profile(video_fps=23.976)
    b = make_profile(video_fps=24.0)
    assert (policies.stream_identity(a, False)
            == policies.stream_identity(b, False))


def test_identity_includes_truncated_fps_when_toggle_on():
    a = make_profile(video_fps=23.976)
    b = make_profile(video_fps=24.0)
    same_rate = make_profile(video_fps=23.5)  # truncates to 23 like 23.976
    assert (policies.stream_identity(a, True)
            != policies.stream_identity(b, True))
    assert (policies.stream_identity(a, True)
            == policies.stream_identity(same_rate, True))


def test_identity_never_includes_incidental_fields():
    a = StreamProfile(hdr_type="hdr10", audio_format="truehd",
                      video_fps=23.976, player_id=1, audio_channels=6)
    b = StreamProfile(hdr_type="hdr10", audio_format="truehd",
                      video_fps=23.976, player_id=2, audio_channels=8)
    for per_fps in (False, True):
        assert (policies.stream_identity(a, per_fps)
                == policies.stream_identity(b, per_fps))


# --- should_apply ------------------------------------------------------------

def test_should_apply_ok():
    assert policies.should_apply(make_profile(),
                                 apply_enabled=True) == (True, None)


def test_should_apply_off_blocks_first():
    # The apply toggle is checked before anything else: with
    # applying off the addon skips regardless of profile state.
    assert policies.should_apply(None,
                                 apply_enabled=False) == (False, "apply_off")
    assert policies.should_apply(make_profile(),
                                 apply_enabled=False) == (False, "apply_off")


def test_should_apply_no_profile():
    assert policies.should_apply(None,
                                 apply_enabled=True) == (False, "no_profile")


def test_should_apply_unknown_format():
    profile = make_profile(audio_format="unknown")
    assert policies.should_apply(
        profile, apply_enabled=True) == (False, "unknown_format")


def test_should_apply_has_no_new_install_gate():
    # An empty store already yields a lookup miss, so a fresh install
    # needs no policy gate.
    import inspect
    signature = inspect.signature(policies.should_apply)
    assert 'new_install' not in signature.parameters
    assert 'hdr_enabled' not in signature.parameters


def test_identity_collapses_spatial_variants_when_distinct_off():
    # With distinct-spatial off a codec and its variant share one key and
    # one offset, so a track switch between them is not a stream change.
    a = make_profile(audio_format="truehd")
    b = make_profile(audio_format="truehd_atmos")
    assert (policies.stream_identity(a, False, False)
            == policies.stream_identity(b, False, False))
    # Composes with the fps axis: still equal at per-fps granularity.
    assert (policies.stream_identity(a, True, False)
            == policies.stream_identity(b, True, False))


def test_identity_keeps_spatial_variants_distinct_by_default():
    # Default mirrors the toggle default (distinct ON), and the identity
    # tracks the lookup key: variant and base are different streams.
    a = make_profile(audio_format="truehd")
    b = make_profile(audio_format="truehd_atmos")
    assert (policies.stream_identity(a, False)
            != policies.stream_identity(b, False))
    assert (policies.stream_identity(a, False, True)
            != policies.stream_identity(b, False, True))


def test_identity_spatial_collapse_leaves_strangers_alone():
    a = make_profile(audio_format="x-future-codec")
    b = make_profile(audio_format="x-future-codec")
    assert (policies.stream_identity(a, False, False)
            == policies.stream_identity(b, False, False))


def test_identity_ignores_channels_by_default():
    # The count is an incidental field until the channel toggle opts in:
    # a wiggle between gathers is not a stream change.
    a = make_profile(audio_channels=8)
    b = make_profile(audio_channels=6)
    assert (policies.stream_identity(a, False)
            == policies.stream_identity(b, False))
    assert (policies.stream_identity(a, False, True, False)
            == policies.stream_identity(b, False, True, False))


def test_identity_includes_the_count_when_channels_toggle_on():
    a = make_profile(audio_channels=8)
    b = make_profile(audio_channels=6)
    same = make_profile(audio_channels=8)
    assert (policies.stream_identity(a, False, True, True)
            != policies.stream_identity(b, False, True, True))
    assert (policies.stream_identity(a, False, True, True)
            == policies.stream_identity(same, False, True, True))


def test_identity_normalizes_unusable_counts_together():
    # Two profiles with unusable counts resolve to the same 'all' key, so
    # they must share an identity — None joins for both.
    a = make_profile(audio_channels='unknown')
    b = make_profile(audio_channels=0)
    assert (policies.stream_identity(a, False, True, True)
            == policies.stream_identity(b, False, True, True))
    c = make_profile(audio_channels=8)
    assert (policies.stream_identity(a, False, True, True)
            != policies.stream_identity(c, False, True, True))


def test_identity_all_three_axes_compose():
    a = make_profile(video_fps=23.976, audio_format="truehd_atmos",
                     audio_channels=8)
    b = make_profile(video_fps=23.976, audio_format="truehd",
                     audio_channels=8)
    # Spatial off + channels on: variant and base share the identity when
    # the counts agree...
    assert (policies.stream_identity(a, True, False, True)
            == policies.stream_identity(b, True, False, True))
    # ...and split when they differ.
    c = make_profile(video_fps=23.976, audio_format="truehd",
                     audio_channels=6)
    assert (policies.stream_identity(a, True, False, True)
            != policies.stream_identity(c, True, False, True))
