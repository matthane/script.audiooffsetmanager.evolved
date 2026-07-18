"""OffsetTable: the sparse-store adapter — the seam the pipeline speaks to.

Its dependencies are the pure store plus one injected settings READ, so it
lives beside the store it adapts. It imports no Kodi module (the settings
adapter is injected), keeping the store package's purity contract intact.

Keys are composed AT CALL TIME from the profile's verbatim facts plus the
LIVE ``per_fps_offsets`` toggle (freshness doctrine: never a captured key,
never conditional on lookup history). Lookup routes through
``resolve.resolve`` (exact -> all -> miss); writes route through
``resolve.write_key`` — the ONLY sanctioned write-key derivation.

The store-entry dict shape stays INSIDE the store package: consumers read
values via ``Resolution.ms`` and ``stored_ms_at`` rather than indexing
``entry['delay_ms']`` themselves.
"""

from resources.lib.aome.store import resolve as store_resolve


class OffsetTable:
    """Adapter over the OffsetStore keyed from profiles + the live toggle."""

    def __init__(self, store, settings):
        self._store = store
        self._settings = settings

    @property
    def read_only(self):
        """True when the store refuses all writes (newer-schema file).

        The watcher checks this in eligibility: a permanently unwritable
        store must stop the learn loop outright instead of re-detecting and
        re-failing the same adjustment every quiescence cycle.
        """
        return self._store.read_only

    def resolve(self, profile):
        """Look up the entry for the profile: a ``resolve.Resolution``."""
        return store_resolve.resolve(
            self._store, profile.hdr_type, profile.video_fps,
            profile.audio_format,
            per_fps=self._settings.per_fps_offsets_enabled())

    def consume_reset(self, key):
        """Discard a pending reset marker (applier acted on it)."""
        return self._store.consume_reset(key)

    def write_key(self, profile):
        """The write key for the profile RIGHT NOW, or None if not
        composable (unparseable fps under per-fps — callers gate on
        completeness first, so this is a belt-and-braces None)."""
        try:
            return store_resolve.write_key(
                profile.hdr_type, profile.video_fps, profile.audio_format,
                per_fps=self._settings.per_fps_offsets_enabled())
        except ValueError:
            return None

    def get_at(self, key):
        """The entry stored at an exact key (or None) — no fallback chain."""
        return self._store.get(key)

    def stored_ms_at(self, key):
        """The verbatim ms stored at an exact key, or None — keeps the
        entry dict shape inside the store package."""
        entry = self._store.get(key)
        if entry is None:
            return None
        return entry['delay_ms']

    def store(self, profile, ms):
        """Store a user adjustment; returns the key written, or None.

        The exact reported rate rides along as entry metadata for the
        management view.
        """
        key = self.write_key(profile)
        if key is None:
            return None
        if not self._store.set(key, ms, video_fps=profile.video_fps):
            return None
        return key
