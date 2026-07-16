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
import math
import os
import time
from datetime import datetime, timezone

_PREFIX = "AOM_OffsetStore:"
_SCHEMA_VERSION = 1


def _noop(_message):
    return None


def _is_int(value):
    """True for real ints only — bool is an int subclass and must not pass.

    The single definition of the doctrine guard, shared by every validation
    site so write-path and load-path rules can never drift apart.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _shape_ok(data):
    """The schema gate shared by load() and the read-only reader."""
    if not isinstance(data, dict):
        return False
    # The schema started at 1: a version of 0 or below never existed and
    # marks a foreign/scribbled file, which must quarantine rather than
    # load-and-resave as if it were current data.
    if not _is_int(data.get("version")) or data.get("version") < 1:
        return False
    if not isinstance(data.get("profiles"), dict):
        return False
    return True


def _load_entries(profiles, log_debug):
    """Filter entries by the doctrine rules; shared with the reader."""
    loaded = {}
    for key, entry in profiles.items():
        if not isinstance(entry, dict):
            log_debug("{0} dropping non-dict entry for {1!r}"
                      .format(_PREFIX, key))
            continue
        delay = entry.get("delay_ms")
        if not _is_int(delay):
            log_debug("{0} dropping entry {1!r} with non-int delay_ms"
                      .format(_PREFIX, key))
            continue
        loaded[key] = dict(entry)
    return loaded


class StoreUnreadable(Exception):
    """The offsets file exists but cannot be presented.

    Raised ONLY by :func:`read_profiles` (the other-process reader): the
    script process must never quarantine or mutate the file, so instead of
    load()'s .bad rename it reports WHY the view cannot render — corrupt
    JSON, wrong shape, an unreadable file, or a newer schema version.

    ``future`` is True for the newer-schema case, which the view must word
    DIFFERENTLY from corruption: the service preserves such a file
    untouched (read-only, "the future is sacred"), it never quarantines it
    (E4 review — the corruption wording falsely promised a reset).
    """

    def __init__(self, message, *, future=False):
        super().__init__(message)
        self.future = future


def read_profiles(path, log_debug=None):
    """Read-only entry snapshot for ANOTHER process (the management view).

    Deliberately NOT OffsetStore.load(): the single-writer doctrine means
    only the service's dispatcher thread may touch the file, so this
    function has no quarantine, no corruption flag, and no instance state —
    it opens, parses, filters (same shape rules as load(), dropped entries
    named through ``log_debug`` just like load() does), and returns.
    A missing file is an empty store ({}); anything unpresentable raises
    :class:`StoreUnreadable`.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise StoreUnreadable("unreadable ({0})".format(error))

    try:
        data = json.loads(raw)
    except ValueError as error:
        raise StoreUnreadable("invalid JSON ({0})".format(error))

    if not _shape_ok(data):
        raise StoreUnreadable("unexpected shape")
    if data["version"] > _SCHEMA_VERSION:
        raise StoreUnreadable(
            "newer schema version {0}".format(data["version"]), future=True)
    return _load_entries(data["profiles"], log_debug or _noop)


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
        return _shape_ok(data)

    def _load_entries(self, profiles):
        return _load_entries(profiles, self._log_debug)

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

    @property
    def read_only(self):
        """True when a newer-schema file was found and all writes are refused.

        Public so the management view can say WHY mutations are refused
        instead of conflating read-only with a persist failure.
        """
        return self._read_only

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
        if not _is_int(delay_ms):
            raise ValueError("delay_ms must be an int")
        if video_fps is not None:
            # Metadata only, but NaN/Infinity would serialize as bare tokens
            # that are not valid JSON for stricter readers — refuse at the
            # door rather than poison the file.
            if isinstance(video_fps, bool) or \
                    not isinstance(video_fps, (int, float)) or \
                    not math.isfinite(video_fps):
                raise ValueError("video_fps must be a finite number or None")

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
        """Remove all entries; return how many were durably removed.

        Persists only when something was actually removed. Refused (returns 0)
        when read-only. A persist failure also returns 0 — the entries would
        resurrect from disk on the next load, and the mutation-channel ack
        must not tell the user "cleared N" when the file still holds them
        (the in-memory removal stands, consistent with set/delete).
        """
        if self._read_only:
            self._log_warning("{0} read-only; refusing clear()".format(_PREFIX))
            return 0
        count = len(self._profiles)
        if count == 0:
            return 0
        self._profiles = {}
        if not self._persist():
            return 0
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
            self._replace_with_retry(tmp)
        except OSError as error:
            self._log_warning("{0} persist failed ({1})".format(_PREFIX, error))
            return False
        return True

    def _replace_with_retry(self, tmp):
        """``os.replace`` with two short retries for Windows share violations.

        On Windows a concurrent reader holding the target open (the script
        process's ``read_profiles`` while the management view renders)
        makes ``os.replace`` fail with a sharing violation. That read
        window is sub-millisecond, so a brief retry closes the race (E4
        review); a persistent failure re-raises into _persist's handler.
        """
        for _attempt in range(2):
            try:
                os.replace(tmp, self._path)
                return
            except OSError:
                time.sleep(0.05)
        os.replace(tmp, self._path)
