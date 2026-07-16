"""Lookup and write-key semantics for the sparse store (the D3/D4 core).

This module IS the key-schema decision table, executable. Both rules are
deliberately trivial — the design work was making them so:

LOOKUP (D3) — both levels are single keys; no scan exists, so no tie-break
can exist:

    per_fps OFF (default):  <hdr>|all|<audio>            -> exact | miss
    per_fps ON:             <hdr>|<fps>|<audio>          -> exact
                            else <hdr>|all|<audio>       -> fallback
                            else                         -> miss

A miss means the caller applies NOTHING (Kodi's delay stays untouched).
Specific-fps entries are dormant while the toggle is OFF; `all` entries
remain reachable while it is ON (as the fallback level) — flipping the
toggle is non-destructive in both directions.

WRITE (D4) — one rule, zero history-dependence: the write key is derived
at store instant from the CURRENT profile facts plus the CURRENT toggle
value, and is never conditional on what any lookup hit. ``hit_kind``
travels for logging/notification wording only. This is the sparse-store
form of the stale-key doctrine: fresh derivation, single writer, no
carried state.

Pure Python: composes ``keys`` and consumes an ``OffsetStore``-shaped
object (``get(key) -> entry | None``); no Kodi anywhere.
"""

from collections import namedtuple

from resources.lib.aom.store import keys

# hit_kind values (travel to logging and notification wording only).
EXACT = 'exact'
FALLBACK = 'fallback'
MISS = 'miss'

# entry: the stored dict (or None on miss); hit_kind: EXACT/FALLBACK/MISS;
# key: the key that hit (or, on a miss, the primary key that was tried —
# useful for the once-per-episode debug line).
Resolution = namedtuple('Resolution', ['entry', 'hit_kind', 'key'])


def resolve(store, hdr_raw, fps, audio_raw, *, per_fps):
    """Look up the offset entry for the given stream facts.

    ``fps`` is only consulted when ``per_fps`` is true (and must then be
    parseable); with the toggle off the fps axis does not exist and the
    single candidate is the ``all`` key.
    """
    if per_fps:
        exact_key = keys.profile_key(hdr_raw, fps, audio_raw, per_fps=True)
        entry = store.get(exact_key)
        if entry is not None:
            return Resolution(entry, EXACT, exact_key)
        fallback_key = keys.all_key(hdr_raw, audio_raw)
        entry = store.get(fallback_key)
        if entry is not None:
            return Resolution(entry, FALLBACK, fallback_key)
        return Resolution(None, MISS, exact_key)

    only_key = keys.all_key(hdr_raw, audio_raw)
    entry = store.get(only_key)
    if entry is not None:
        return Resolution(entry, EXACT, only_key)
    return Resolution(None, MISS, only_key)


def write_key(hdr_raw, fps, audio_raw, *, per_fps):
    """The single key a manual adjustment is stored under (D4).

    Derived from the CURRENT profile facts + CURRENT toggle at the moment
    of storing — never from lookup history. With the toggle off this is
    the ``all`` key; with it on, the fps-specific key.
    """
    return keys.profile_key(hdr_raw, fps, audio_raw, per_fps=per_fps)
