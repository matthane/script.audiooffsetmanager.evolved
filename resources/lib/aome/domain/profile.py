"""Immutable stream profile — verbatim detection facts.

Pure data under the open format vocabulary: the axes carry
what Kodi REPORTED, normalized only by case-fold/trim (plus the detector's
sdr default for an absent HDR axis). No whitelist shapes these fields, and
no settings key is derived here — the store key is composed at lookup/write
instant by ``aome.store`` (per_fps toggle consulted there, never captured).

Display formatting deliberately does NOT live here: the toast/view layers
format via ``aome.store.keys`` display helpers, keeping this module free of
vocabulary tables (domain purity: this file imports nothing).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StreamProfile:
    """Immutable stream characteristics, exactly as detected."""

    hdr_type: str        # verbatim segment; 'sdr' default applied by detector
    audio_format: str    # verbatim segment, or 'unknown' when unreported
    video_fps: object    # float | None — the exact reported rate
    player_id: int
    audio_channels: object

    def fps_int(self):
        """The fps key axis: integer truncation of the reported rate.

        Truncation — not rounding — is what keeps the NTSC fractional rates
        on their own keys (23.976 -> 23 vs 24.0 -> 24).
        None when the rate was not detected.
        """
        if self.video_fps is None:
            return None
        return int(self.video_fps)

    def identity(self):
        """Offset-relevant identity: the tuple two gathers must share to be
        "the same stream" at full granularity.

        Incidental fields (player_id, audio_channels) are excluded on
        purpose — they can wiggle between gathers without the stream
        changing for offset purposes. Callers that should ignore the fps
        axis (per_fps toggle OFF) use ``policies.stream_identity`` instead.
        """
        return (self.hdr_type, self.fps_int(), self.audio_format)

    def describe(self):
        """Compact greppable form for field logs: ``hdr|fps|audio``."""
        fps = self.fps_int()
        return "{0}|{1}|{2}".format(
            self.hdr_type, '?' if fps is None else fps, self.audio_format)
