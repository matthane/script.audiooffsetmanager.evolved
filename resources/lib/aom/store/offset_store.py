"""OffsetStore: the sparse JSON offset database (addon_data/offsets.json).

Evolved keeps learned audio offsets here, keyed by stream profile, instead of
in settings.xml. This module is the whole persistence surface for that file.

Design decisions worth stating up front:

* **Single-writer doctrine, no locking.** At runtime the store is owned by the
  one dispatcher thread; every read and mutation happens there. The class is
  therefore plain synchronous Python with no locks, no threads, and no global
  state — the injected file path, clock, and log sinks are all it depends on.
* **Verbatim signed integers.** ``delay_ms`` is a signed integer at 1 ms
  resolution and is stored EXACTLY as given. There is deliberately NO step /
  increment snapping (no 25 ms or 5 ms quantization) and NO range clamping
  (not ±1000, not ±10000) — deciding what values are legal is the caller's
  parser's job, not the store's. Any ``int`` round-trips bit-for-bit.
* **Corruption is survivable, the future is sacred.** A file we cannot parse
  is moved aside to ``<path>.bad`` and we start empty (data loss is preferable
  to a jammed addon). A file from a NEWER schema is left completely untouched
  and the store goes read-only, so an older build downgraded onto newer data
  can never clobber it.
* **Atomic-swap durability.** Every persist writes a sibling ``.tmp`` file,
  flushes and ``fsync``s it, then ``os.replace``s it over the target — the
  swap is atomic on POSIX and NTFS, so a power loss on an HTPC box leaves
  either the old file or the new one, never a half-written one.

No disk I/O happens in the constructor; ``load()`` is the single explicit read.
"""

import json
import os
import time
from datetime import datetime, timezone

_PREFIX = "AOM_OffsetStore:"
_SCHEMA_VERSION = 1


def _noop(_message):
    return None


class OffsetStore:
    """Sparse per-profile offset database backed by a single JSON file."""

    def __init__(self, path, *, clock=time.time, log_debug=None, log_warning=None):
        self._path = path
        self._clock = clock
        self._log_debug = log_debug or _noop
        self._log_warning = log_warning or _noop
        self._profiles = {}
        self._read_only = False
        self._corruption = False

    # -- loading --------------------------------------------------------------

    def load(self):
        """Read the file once, populating in-memory state.

        Missing file → empty and writable (normal first run). Unreadable or
        malformed → the file is renamed to ``<path>.bad`` and we start empty,
        writable, with the corruption flag set. A future schema version → the
        file is left untouched and the store becomes read-only.
        """
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except FileNotFoundError:
            # Normal first run: nothing on disk yet.
            self._profiles = {}
            return
        except OSError as error:
            self._log_warning("{0} unreadable ({1}); quarantining"
                              .format(_PREFIX, error))
            self._quarantine()
            return

        try:
            data = json.loads(raw)
        except ValueError as error:
            self._log_warning("{0} invalid JSON ({1}); quarantining"
                              .format(_PREFIX, error))
            self._quarantine()
            return

        if not self._shape_ok(data):
            self._log_warning("{0} unexpected shape; quarantining".format(_PREFIX))
            self._quarantine()
            return

        version = data["version"]
        if version > _SCHEMA_VERSION:
            # A newer build wrote this. Leave it exactly as-is and refuse all
            # writes so a downgrade can never overwrite newer data.
            self._read_only = True
            self._profiles = {}
            self._log_warning("{0} file version {1} > {2}; read-only"
                              .format(_PREFIX, version, _SCHEMA_VERSION))
            return

        self._profiles = self._load_entries(data["profiles"])

    def _shape_ok(self, data):
        if not isinstance(data, dict):
            return False
        if not isinstance(data.get("version"), int) or isinstance(data.get("version"), bool):
            return False
        if not isinstance(data.get("profiles"), dict):
            return False
        return True

    def _load_entries(self, profiles):
        loaded = {}
        for key, entry in profiles.items():
            if not isinstance(entry, dict):
                self._log_debug("{0} dropping non-dict entry for {1!r}"
                                .format(_PREFIX, key))
                continue
            delay = entry.get("delay_ms")
            if not isinstance(delay, int) or isinstance(delay, bool):
                self._log_debug("{0} dropping entry {1!r} with non-int delay_ms"
                                .format(_PREFIX, key))
                continue
            loaded[key] = dict(entry)
        return loaded

    def _quarantine(self):
        """Move a corrupt file aside and start empty but writable."""
        bad = self._path + ".bad"
        try:
            os.replace(self._path, bad)
        except OSError as error:
            # Losing the quarantine rename is non-fatal: we still start empty.
            self._log_warning("{0} could not rename to .bad ({1})"
                              .format(_PREFIX, error))
        self._profiles = {}
        self._corruption = True

    def pop_corruption(self):
        """Return the corruption flag and clear it (one-shot for the notifier)."""
        flagged = self._corruption
        self._corruption = False
        return flagged

    # -- reads ----------------------------------------------------------------

    def get(self, key):
        """Return a COPY of the entry for ``key`` (or None) — never the live dict."""
        entry = self._profiles.get(key)
        if entry is None:
            return None
        return dict(entry)

    def entries(self):
        """Snapshot: ``{key: entry_copy}`` for every stored profile."""
        return {key: dict(entry) for key, entry in self._profiles.items()}

    def __len__(self):
        return len(self._profiles)

    # -- writes ---------------------------------------------------------------

    def set(self, key, delay_ms, *, source="user", video_fps=None):
        """Store an offset for ``key``, persist, and report success.

        ``key`` must be a non-empty str and ``delay_ms`` an int (bool rejected);
        violating either raises ValueError — that is a programmer error, not a
        runtime condition. A fresh ``updated`` timestamp is stamped every call.
        Returns False (after a warning) if the store is read-only or the write
        fails; True otherwise.
        """
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty str")
        if not isinstance(delay_ms, int) or isinstance(delay_ms, bool):
            raise ValueError("delay_ms must be an int")

        if self._read_only:
            self._log_warning("{0} read-only; refusing set({1!r})"
                              .format(_PREFIX, key))
            return False

        entry = {
            "delay_ms": delay_ms,
            "updated": self._timestamp(),
            "source": source,
        }
        if video_fps is not None:
            entry["video_fps"] = video_fps

        self._profiles[key] = entry
        if not self._persist():
            return False
        return True

    def delete(self, key):
        """Remove ``key`` if present and persist; True only when both happen.

        A miss touches no disk (returns False). Refused when read-only. A
        persist failure also returns False — the entry would resurrect from
        disk on the next load, and the mutation-channel ack must not claim
        otherwise (the removal stays in memory, consistent with ``set``).
        """
        if self._read_only:
            self._log_warning("{0} read-only; refusing delete({1!r})"
                              .format(_PREFIX, key))
            return False
        if key not in self._profiles:
            return False
        del self._profiles[key]
        return self._persist()

    def clear(self):
        """Remove all entries; return how many were removed.

        Persists only when something was actually removed. Refused (returns 0)
        when read-only.
        """
        if self._read_only:
            self._log_warning("{0} read-only; refusing clear()".format(_PREFIX))
            return 0
        count = len(self._profiles)
        if count == 0:
            return 0
        self._profiles = {}
        self._persist()
        return count

    # -- internals ------------------------------------------------------------

    def _timestamp(self):
        moment = datetime.fromtimestamp(self._clock(), timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _persist(self):
        """Serialize and atomically swap the file into place.

        On OSError the in-memory state is left AS MUTATED (the caller already
        acted on the new value; rolling back the dict would surprise a
        single-threaded caller more than a stale-on-disk file does) and False
        is returned so the caller can react to the durability miss.
        """
        payload = {"version": _SCHEMA_VERSION, "profiles": self._profiles}
        blob = json.dumps(payload, indent=2, sort_keys=True)
        tmp = self._path + ".tmp"
        try:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except OSError as error:
            self._log_warning("{0} persist failed ({1})".format(_PREFIX, error))
            return False
        return True
