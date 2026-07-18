"""The stream-classification absence sentinel.

Formats are accepted verbatim — the reported string IS the store-key
segment — so there is no format vocabulary to enumerate here. Display
names live with the key codec in ``aome.store.keys`` (the one display
table in the addon).

This module holds the single cross-layer sentinel for "this axis could not
be detected". ``aome.store.keys`` imports it (absence normalization), the
detector stamps it, and ``policies.is_complete`` gates on it.

Pure Python: no Kodi imports.
"""

# Sentinel for any axis that could not be detected. Also the absence KEY
# SEGMENT: keys.audio_segment/hdr_segment normalize ''/'none'/'unknown'
# to this value, so it appears verbatim inside store keys.
UNKNOWN = 'unknown'
