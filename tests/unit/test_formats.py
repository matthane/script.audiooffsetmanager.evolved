"""Unit tests for aom.domain.formats — now just the UNKNOWN sentinel.

The classic vocabulary tables (HDR/audio/fps tuples, display names,
settings-id tables, the 315-key generator feed) died with the offset matrix
in E3: Evolved accepts formats verbatim (D11), so there is no closed
vocabulary to pin. What's left to protect is the sentinel's value (it
appears verbatim inside store keys on disk), the demolition itself, and the
detector's verbatim handling of the names the classic vocabulary used to
gatekeep.
"""

from resources.lib.aom.domain import formats


def test_unknown_sentinel_value_is_frozen():
    # UNKNOWN is not just a Python sentinel: keys.audio_segment/hdr_segment
    # write it into on-disk store keys (the absence rule), so changing the
    # string would orphan every stored '...|unknown' entry.
    assert formats.UNKNOWN == 'unknown'


def test_matrix_vocabulary_is_gone():
    # D11 verbatim acceptance: any enumerated vocabulary reappearing here
    # is design regression toward whitelist matching (EVOLVED.md edit
    # required first).
    for dead in ('HDR_TYPES', 'AUDIO_FORMATS', 'FPS_BUCKETS', 'FPS_ALL',
                 'HDR_DISPLAY_NAMES', 'AUDIO_DISPLAY_NAMES',
                 'FPS_DISPLAY_NAMES', 'AUDIO_STRING_IDS',
                 'HDR_ENABLE_STRING_IDS', 'HDR_CATEGORY_LABELS',
                 'HDR_GROUP_IDS', 'FPS_SPINNER_STRING_IDS',
                 'FPS_OPTION_LABEL_IDS', 'setting_key', 'all_setting_keys'):
        assert not hasattr(formats, dead)


def test_classic_vocabulary_still_round_trips_verbatim():
    # The classic closed vocabulary (inlined — the enumerating tuple is
    # gone) plus strangers it used to reject: all pass through the detector
    # verbatim now. The tables were gatekeepers; verbatim acceptance keeps
    # their NAMES working without keeping the gate.
    from resources.lib.aom.app.stream_detector import derive_stream_facts
    classic = ('truehd', 'eac3', 'ac3', 'dtshd_ma', 'dtshd_hra', 'dca', 'pcm')
    for audio in classic + ('x-future-codec', 'aac'):
        facts = derive_stream_facts(
            player_id=1, raw_codec=audio, raw_channels=6,
            raw_fps='23.976', raw_hdr='hdr10', raw_hdr_fallback='',
            raw_gamut='')
        assert facts.profile.audio_format == audio
        assert facts.profile.hdr_type == 'hdr10'
        assert facts.profile.video_fps == 23.976
