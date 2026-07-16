"""Lookup and write-key semantics for the sparse store (the D3/D4 core).

This module IS the key-schema decision table, executable. Both rules are
deliberately trivial — the design work was making them so:

LOOKUP (D3) — both levels are single keys; no scan exists, so no tie-break
can exist:

    per_fps OFF (default):  <hdr>|all|<audio>            -> exact | miss
    per_fps ON:             <hdr>|<fps>|<audio>          -> exact
                            else <hdr>|all|<audio>       -> fallback
                            else                         -> miss

A miss means the caller applies NOTHING (Kodi's delay stays untouched) —
UNLESS a consulted key carries a reset marker (the user deleted it; D3
second amendment, E7): ``reset_keys`` names every consulted key with a
pending reset so the applier can force the 0 the deletion promised. A key
consulted BEFORE a hit can carry a marker too (deleted exact entry over a
kept ``all`` fallback); the hit wins — the fallback was kept deliberately
and its apply overwrites any residue — and the applier consumes the stale
marker silently. Specific-fps entries are dormant while the toggle is
OFF; `all` entries remain reachable while it is ON (as the fallback
level) — flipping the toggle is non-destructive in both directions.

WRITE (D4) — one rule, zero history-dependence: the write key is derived
at store instant from the CURRENT profile facts plus the CURRENT toggle
value, and is never conditional on what any lookup hit. ``hit_kind``
travels for logging/notification wording only. This is the sparse-store
form of the stale-key doctrine: fresh derivation, single writer, no
carried state.

Pure Python: composes ``keys`` and consumes an ``OffsetStore``-shaped
object (``get(key) -> entry | None``, ``reset_pending(key) -> bool``);
no Kodi anywhere.
"""

from collections import namedtuple

from resources.lib.aom.store import keys

# hit_kind values (travel to logging and notification wording only).
EXACT = 'exact'
FALLBACK = 'fallback'
MISS = 'miss'

# entry: the stored dict, or None on a miss.
# hit_kind: EXACT / FALLBACK / MISS.
# key: the key that HIT — None on a miss (one stable meaning; no
#      hit/miss-dependent referent).
# tried: every key consulted, in lookup order — the once-per-episode debug
#        line logs this so a diagnostician sees the whole chain that missed.
# reset_keys: consulted keys carrying a pending reset marker, in lookup
#             order — non-empty on a miss means "force 0, not no-op".
#             Defaulted to () so hand-built Resolutions (tests, seams)
#             stay valid; resolve() always fills it explicitly.
class Resolution(namedtuple('Resolution',
                            ['entry', 'hit_kind', 'key', 'tried',
                             'reset_keys'],
                            defaults=((),))):
    __slots__ = ()

    @property
    def ms(self):
        """The verbatim stored ms, or None on a miss — consumers read this
        instead of indexing the entry dict (the dict shape stays inside the
        store package)."""
        if self.entry is None:
            return None
        return self.entry['delay_ms']


def resolve(store, hdr_raw, fps, audio_raw, *, per_fps):
    """Look up the offset entry for the given stream facts. Total: never raises.

    With the toggle off the fps axis does not exist and the single candidate
    is the ``all`` key (``fps`` is not even parsed). With it on, the exact
    key is tried first, then the ``all`` key; an fps that cannot be parsed
    simply means the exact LEVEL is unavailable — the lookup degrades to the
    fallback level rather than turning a benign miss into an exception.
    """
    if not per_fps:
        only_key = keys.all_key(hdr_raw, audio_raw)
        entry = store.get(only_key)
        if entry is not None:
            return Resolution(entry, EXACT, only_key, (only_key,), ())
        return Resolution(None, MISS, None, (only_key,),
                          _pending((only_key,), store))

    tried = []
    try:
        exact_key = keys.profile_key(hdr_raw, fps, audio_raw, per_fps=True)
    except ValueError:
        exact_key = None  # exact level unavailable (unparseable fps)
    if exact_key is not None:
        tried.append(exact_key)
        entry = store.get(exact_key)
        if entry is not None:
            return Resolution(entry, EXACT, exact_key, tuple(tried), ())

    fallback_key = keys.all_key(hdr_raw, audio_raw)
    tried.append(fallback_key)
    entry = store.get(fallback_key)
    if entry is not None:
        # A marker on the exact level (deleted) under a kept fallback: the
        # hit wins, the marker travels for silent consumption.
        return Resolution(entry, FALLBACK, fallback_key, tuple(tried),
                          _pending(tried[:-1], store))
    return Resolution(None, MISS, None, tuple(tried),
                      _pending(tried, store))


def _pending(consulted, store):
    """The consulted keys carrying reset markers, in lookup order."""
    return tuple(key for key in consulted if store.reset_pending(key))


def write_key(hdr_raw, fps, audio_raw, *, per_fps):
    """The single key a manual adjustment is stored under (D4).

    Derived from the CURRENT profile facts + CURRENT toggle at the moment
    of storing — never from lookup history. With the toggle off this is
    the ``all`` key; with it on, the fps-specific key.
    """
    return keys.profile_key(hdr_raw, fps, audio_raw, per_fps=per_fps)
