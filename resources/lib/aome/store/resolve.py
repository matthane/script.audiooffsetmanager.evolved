"""Lookup and write-key semantics for the sparse store.

This module IS the key-schema decision table, executable. Both rules are
deliberately trivial — the design work was making them so:

LOOKUP — one candidate key per mode, symmetric in both directions:

    per_fps OFF (default):  <hdr>|all|<audio>            -> exact | miss
    per_fps ON:             <hdr>|<fps>|<audio>          -> exact | miss

There is NO fallback between the levels: an offset applies only in the
mode it was saved in. Specific-fps entries are dormant while the toggle
is OFF; ``all`` entries are dormant while it is ON — flipping the toggle
is non-destructive in both directions, and each mode's lookup consults
only its own key. One deliberate seam in the symmetry: an fps that
cannot be parsed under the toggle means the stream has NO fps axis, so
its candidate is the ``all`` key — the same meaning ``all`` carries when
the toggle is off (defensive only: completeness gating keeps unparseable
rates out of the production apply path).

A miss means the caller applies NOTHING (Kodi's delay stays untouched) —
UNLESS the consulted key carries a reset marker (the user deleted it):
``reset_keys`` names it so the applier can force the 0 the deletion
promised. A hit can never carry a marker: the store supersedes a key's
marker whenever a value is set, and the single-candidate lookup consults
no other key.

WRITE — one rule, zero history-dependence: the write key is derived
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

from resources.lib.aome.store import keys

# hit_kind values (travel to logging and notification wording only).
EXACT = 'exact'
MISS = 'miss'

# entry: the stored dict, or None on a miss.
# hit_kind: EXACT / MISS.
# key: the key that HIT — None on a miss (one stable meaning; no
#      hit/miss-dependent referent).
# tried: the consulted key, as a 1-tuple — the once-per-episode debug
#        line logs this so a diagnostician sees exactly what missed.
# reset_keys: consulted keys carrying a pending reset marker —
#             non-empty on a miss means "force 0, not no-op".
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

    Exactly one candidate key exists per call: the ``all`` key with the
    toggle off (``fps`` is not even parsed), the fps-specific key with it
    on. An fps that cannot be parsed under the toggle means the stream
    has no fps axis, so its candidate degrades to the ``all`` key rather
    than turning a benign miss into an exception.
    """
    if not per_fps:
        candidate = keys.all_key(hdr_raw, audio_raw)
    else:
        try:
            candidate = keys.profile_key(hdr_raw, fps, audio_raw,
                                         per_fps=True)
        except ValueError:
            # No fps axis on this stream: the all key IS its exact key.
            candidate = keys.all_key(hdr_raw, audio_raw)
    entry = store.get(candidate)
    if entry is not None:
        return Resolution(entry, EXACT, candidate, (candidate,), ())
    return Resolution(None, MISS, None, (candidate,),
                      _pending((candidate,), store))


def _pending(consulted, store):
    """The consulted keys carrying reset markers, in lookup order."""
    return tuple(key for key in consulted if store.reset_pending(key))


def write_key(hdr_raw, fps, audio_raw, *, per_fps):
    """The single key a manual adjustment is stored under.

    Derived from the CURRENT profile facts + CURRENT toggle at the moment
    of storing — never from lookup history. With the toggle off this is
    the ``all`` key; with it on, the fps-specific key.
    """
    return keys.profile_key(hdr_raw, fps, audio_raw, per_fps=per_fps)
