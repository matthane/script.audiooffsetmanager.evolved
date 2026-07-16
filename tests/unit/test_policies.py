"""Unit tests for aom.domain.policies — parsing, completeness, gating.

parse_delay_ms started as a verbatim move of ActiveMonitor.convert_delay_to_ms
(full locale/clamping matrix in tests/unit/test_delay_parsing.py); Phase 6
fixed its two pinned limitations — the NNBSP-as-sole-separator parse failure
and the int() ms truncation — and flipped the pins here and there.
"""

import pytest

from resources.lib.aom.domain import policies
from resources.lib.aom.domain.profile import StreamProfile

NNBSP = " "


def make_profile(hdr_type="hdr10", audio_format="truehd", video_fps=23.976):
    return StreamProfile(
        hdr_type=hdr_type,
        audio_format=audio_format,
        video_fps=video_fps,
        player_id=1,
        audio_channels=6,
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
    # Phase 6 fix (flipped pin): NNBSP directly against the unit — the CLDR
    # unit-separator convention — parses like any other separator.
    assert policies.parse_delay_ms("-0.075" + NNBSP + "s") == -75


def test_parse_delay_ms_unicode_minus_sign():
    # Phase 6 review fix: some CLDR locales render negatives with U+2212.
    assert policies.parse_delay_ms("−0.075 s") == -75


def test_parse_delay_ms_rounds_instead_of_truncating():
    # Phase 6 fix: float('-0.115') * 1000 is -114.999...; int() used to
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
    assert policies.should_apply(make_profile(), paused=False) == (True, None)


def test_should_apply_paused_blocks_first():
    # The global pause is checked before anything else (D9): a paused addon
    # skips regardless of profile state.
    assert policies.should_apply(None, paused=True) == (False, "paused")
    assert policies.should_apply(make_profile(),
                                 paused=True) == (False, "paused")


def test_should_apply_no_profile():
    assert policies.should_apply(None, paused=False) == (False, "no_profile")


def test_should_apply_unknown_format():
    profile = make_profile(audio_format="unknown")
    assert policies.should_apply(profile,
                                 paused=False) == (False, "unknown_format")


def test_should_apply_has_no_new_install_gate():
    # P1: the onboarding gate is deleted, not ported — an empty store
    # already yields a lookup miss, so a fresh install needs no policy gate.
    import inspect
    signature = inspect.signature(policies.should_apply)
    assert 'new_install' not in signature.parameters
    assert 'hdr_enabled' not in signature.parameters
