"""Immutable stream profile: the detection facts, exactly as Kodi reported.

The axes carry what Kodi reported, normalized by ``aome.store.keys``
segment rules. No store key is derived here; it is composed at lookup/write
time by ``aome.store`` (which consults the per_fps toggle then). Display
formatting also lives in ``aome.store.keys``, keeping this module a pure
data class that imports nothing.
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
        """The fps key axis: the reported rate truncated to an int, or None.

        Truncation (not rounding) keeps NTSC fractional rates on their own
        keys: 23.976 -> 23 stays distinct from 24.0 -> 24.
        """
        if self.video_fps is None:
            return None
        return int(self.video_fps)

    def identity(self):
        """The tuple two gathers must share to be "the same stream".

        Incidental fields (player_id, audio_channels) are excluded: they can
        wiggle between gathers without the stream changing for offset
        purposes. Callers that ignore the fps axis (per_fps off) use
        ``policies.stream_identity`` instead.
        """
        return (self.hdr_type, self.fps_int(), self.audio_format)

    def describe(self):
        """Compact greppable form for logs: ``hdr|fps|audio``."""
        fps = self.fps_int()
        return "{0}|{1}|{2}".format(
            self.hdr_type, '?' if fps is None else fps, self.audio_format)
