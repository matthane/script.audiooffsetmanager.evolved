"""The stream-classification absence sentinel.

Classic AOM enumerated its whole format vocabulary here (HDR types, audio
formats, fps buckets, display names, settings-id tables) because the offset
matrix in settings.xml needed a closed, ordered list to generate against.
Evolved accepts formats verbatim (D11): the reported string IS the store-key
segment, so there is no vocabulary to enumerate — the tables died with the
matrix and the generator in E3. Display names live with the key codec in
``aome.store.keys`` (the ONE surviving table).

What remains is the single cross-layer sentinel for "this axis could not be
detected". ``aome.store.keys`` imports it (absence normalization), the
detector stamps it, and ``policies.is_complete`` gates on it.

Pure Python: no Kodi imports.
"""

# Sentinel for any axis that could not be detected. Also the absence KEY
# SEGMENT: keys.audio_segment/hdr_segment normalize ''/'none'/'unknown'
# to this value, so it appears verbatim inside store keys.
UNKNOWN = 'unknown'
