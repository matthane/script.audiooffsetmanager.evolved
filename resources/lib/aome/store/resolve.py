"""Lookup and write-key semantics for the sparse store.

This module is the key-schema decision table. Both rules are deliberately
trivial:

Lookup — one candidate key per call, composed from both granularity modes:

    per_fps off (default):  <hdr>|all|<audio>   -> exact | miss
    per_fps on:             <hdr>|<fps>|<audio> -> exact | miss

    distinct_spatial on (default): <audio> is the verbatim segment
    distinct_spatial off:          <audio> is the variant's base codec

There is no fallback between the levels. The fps modes are symmetric:
specific-fps entries are dormant while the toggle is off, ``all`` entries
while it is on. The spatial modes are one-sided: a base-codec key (e.g.
``truehd``) is a legitimate verbatim key in both modes, so only
spatial-variant entries (``truehd_atmos``) go dormant, and only while
distinct_spatial is off. Flipping either toggle is non-destructive. One
seam: an fps that cannot be parsed under the toggle means the stream has no
fps axis, so its candidate is the ``all`` key (defensive only; completeness
gating keeps unparseable rates out of the apply path).

A miss applies nothing (Kodi's delay stays untouched) unless the consulted
key carries a reset marker (the user deleted it): ``reset_keys`` names it so
the applier can force the promised 0. A hit never carries a marker — the
store supersedes a key's marker on set, and the single-candidate lookup
consults no other key.

Write — one rule, no history-dependence: the write key is derived at store
instant from the current profile facts plus the current toggle, never
conditional on what a lookup hit. ``hit_kind`` travels for logging and
notification wording only.

Pure Python: composes ``keys`` and consumes an ``OffsetStore``-shaped object
(``get(key) -> entry | None``, ``reset_pending(key) -> bool``); no Kodi.
"""

from collections import namedtuple

from resources.lib.aome.store import keys

# hit_kind values (travel to logging and notification wording only).
EXACT = 'exact'
MISS = 'miss'

# entry: the stored dict, or None on a miss.
# hit_kind: EXACT / MISS.
# key: the key that hit, or None on a miss.
# tried: the consulted key as a 1-tuple, logged so a miss shows what missed.
# reset_keys: consulted keys carrying a pending reset marker; non-empty on
#             a miss means "force 0, not no-op". Defaulted to () so
#             hand-built Resolutions stay valid; resolve() always fills it.
class Resolution(namedtuple('Resolution',
                            ['entry', 'hit_kind', 'key', 'tried',
                             'reset_keys'],
                            defaults=((),))):
    __slots__ = ()

    @property
    def ms(self):
        """The stored ms, or None on a miss (keeps the entry dict shape
        inside the store package)."""
        if self.entry is None:
            return None
        return self.entry['delay_ms']


def resolve(store, hdr_raw, fps, audio_raw, *, per_fps, distinct_spatial):
    """Look up the offset entry for the given stream facts; never raises.

    Exactly one candidate key per call: the ``all`` key with per_fps off,
    the fps-specific key with it on; the audio segment collapses to its
    spatial base with distinct_spatial off. An unparseable fps under
    per_fps degrades to the ``all`` key rather than turning a benign miss
    into an exception.
    """
    if not per_fps:
        candidate = keys.all_key(hdr_raw, audio_raw,
                                 distinct_spatial=distinct_spatial)
    else:
        try:
            candidate = keys.profile_key(hdr_raw, fps, audio_raw,
                                         per_fps=True,
                                         distinct_spatial=distinct_spatial)
        except ValueError:
            # No fps axis on this stream: the all key IS its exact key.
            candidate = keys.all_key(hdr_raw, audio_raw,
                                     distinct_spatial=distinct_spatial)
    entry = store.get(candidate)
    if entry is not None:
        return Resolution(entry, EXACT, candidate, (candidate,), ())
    return Resolution(None, MISS, None, (candidate,),
                      _pending((candidate,), store))


def _pending(consulted, store):
    """The consulted keys carrying reset markers, in lookup order."""
    return tuple(key for key in consulted if store.reset_pending(key))


def write_key(hdr_raw, fps, audio_raw, *, per_fps, distinct_spatial):
    """The single key a manual adjustment is stored under.

    Derived from the current profile facts and toggles at store time, never
    from lookup history: the ``all`` key with per_fps off, the fps-specific
    key with it on, the spatial-base audio segment with distinct_spatial
    off.
    """
    return keys.profile_key(hdr_raw, fps, audio_raw, per_fps=per_fps,
                            distinct_spatial=distinct_spatial)
