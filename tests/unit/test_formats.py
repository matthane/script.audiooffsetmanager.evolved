"""Unit tests for aome.domain.formats — now just the UNKNOWN sentinel.

Formats are accepted verbatim, so there is no closed vocabulary to pin.
What this suite protects is the sentinel's value (it appears verbatim
inside store keys on disk), the absence of any enumerated vocabulary, and
the detector's verbatim handling of common codec names.
"""

from resources.lib.aome.domain import formats


def test_unknown_sentinel_value_is_frozen():
    # UNKNOWN is not just a Python sentinel: keys.audio_segment/hdr_segment
    # write it into on-disk store keys (the absence rule), so changing the
    # string would orphan every stored '...|unknown' entry.
    assert formats.UNKNOWN == 'unknown'


def test_matrix_vocabulary_is_gone():
    # Verbatim acceptance: any enumerated vocabulary reappearing here
    # is design regression toward whitelist matching.
    for dead in ('HDR_TYPES', 'AUDIO_FORMATS', 'FPS_BUCKETS', 'FPS_ALL',
                 'HDR_DISPLAY_NAMES', 'AUDIO_DISPLAY_NAMES',
                 'FPS_DISPLAY_NAMES', 'AUDIO_STRING_IDS',
                 'HDR_ENABLE_STRING_IDS', 'HDR_CATEGORY_LABELS',
                 'HDR_GROUP_IDS', 'FPS_SPINNER_STRING_IDS',
                 'FPS_OPTION_LABEL_IDS', 'setting_key', 'all_setting_keys'):
        assert not hasattr(formats, dead)


def test_common_codec_names_round_trip_verbatim():
    # Common codec names plus strangers: all pass through the detector
    # verbatim.
    from resources.lib.aome.app.stream_detector import derive_stream_facts
    common = ('truehd', 'eac3', 'ac3', 'dtshd_ma', 'dtshd_hra', 'dca', 'pcm')
    for audio in common + ('x-future-codec', 'aac'):
        facts = derive_stream_facts(
            player_id=1, raw_codec=audio, raw_channels=6,
            raw_fps='23.976', raw_hdr='hdr10', raw_hdr_fallback='',
            raw_gamut='')
        assert facts.profile.audio_format == audio
        assert facts.profile.hdr_type == 'hdr10'
        assert facts.profile.video_fps == 23.976


# --- the spatial-variant fact table ------------------------------------------

def test_spatial_base_pins_the_observed_variant_spellings():
    # Exactly the variant cases in Kodi's StreamUtils::GetCodecName — the
    # never-speculative rule forbids padding this map. Lossy DTS:X over
    # HRA reports as plain 'dtshd_hra' (FFmpeg reads the X syncword only
    # inside the lossless XLL substream), so it has no spelling to map.
    assert formats.SPATIAL_BASE == {
        'truehd_atmos': 'truehd',
        'eac3_ddp_atmos': 'eac3',
        'dtshd_ma_x': 'dtshd_ma',
        'dtshd_ma_x_imax': 'dtshd_ma',
    }


def test_spatial_base_passes_everything_else_through():
    # Not a whitelist: a base codec, a stranger, and the absence sentinel
    # all pass through unchanged.
    for segment in ('truehd', 'eac3', 'dtshd_ma', 'dtshd_hra',
                    'x-future-codec', formats.UNKNOWN):
        assert formats.spatial_base(segment) == segment
