"""The "axis could not be detected" sentinel, shared across layers.

Formats are accepted verbatim (the reported string is the store-key
segment), so there is no format vocabulary to enumerate here. Display
names live with the key codec in ``aome.store.keys``.

Pure Python: no Kodi imports.
"""

# Sentinel for any undetected axis, and the absence key segment:
# keys.audio_segment/hdr_segment normalize ''/'none'/'unknown' to this.
UNKNOWN = 'unknown'
