"""ManageView: the script-process stored-offsets management surface.

This is the user-facing half of the store mutation channel whose service
half is :mod:`resources.lib.aom.app.store_mutations`. It runs in the SCRIPT
process and honours the same P6 boundary from the other side: inspection
plus delete/clear ONLY. There is no value entry anywhere in this module —
offsets are learned during playback (the watcher), never typed here — and
it NEVER writes the store file. It READS the file directly through the
injected read-only reader and asks the service to mutate over the channel.

The seam is three injected callables, wired by the script router:

* ``read_entries()`` returns a ``{key: entry}`` snapshot (each entry has
  ``delay_ms``, ``updated``, ``source``, optional ``video_fps``) and may
  raise :class:`StoreUnreadable` — the file exists but cannot be presented.
* ``gui`` is the plain-dialog surface (``select``/``yesno``/``ok`` +
  ``localized``); ``select`` returns the chosen index or -1 on cancel.
* ``send_mutation(op, key=None)`` posts a delete/clear over the channel and
  returns the service's ack dict, or ``None`` on timeout — the D5
  report-only signal that the service is not running. There is deliberately
  NO fallback write path: a missing service is reported, never worked around.

``run()`` is a re-read-and-render loop. Every pass reads the store fresh, so
a delete's effect is the next render (a cleared store lands on the empty
state and exits) — the refreshed list IS the feedback. Values render
VERBATIM: the odd signed millisecond integers the store keeps (-115, +9999)
are shown exactly, never rounded or step-snapped. The empty state is the
first-run education: nothing is stored until the user fixes lipsync once.
"""

from collections import namedtuple

from resources.lib.aom.store.keys import describe_key
from resources.lib.aom.store.offset_store import StoreUnreadable

# Localized string ids owned by this view (defined in strings.po).
_HEADING = 32115           # "Manage stored offsets"
_MSG_EMPTY = 32122         # first-run education / empty store
_MSG_CONFIRM_DELETE = 32123
_MSG_CONFIRM_CLEAR = 32124
_MSG_NO_SERVICE = 32125    # ack timeout: service not running
_LABEL_CLEAR_ALL = 32126
_MSG_UNREADABLE = 32127    # StoreUnreadable (corrupt: will be quarantined)
_MSG_MUTATION_FAILED = 32128
_MSG_FUTURE = 32131        # StoreUnreadable(future=True): preserved, not shown
_LABEL_BACK = 32134        # labeled exit row ("Back to settings")

# English fallbacks for the dialogs whose ENTIRE content is one localized
# string: localized() degrades to '' on a transient failure, and a blank
# information dialog teaches nothing (same doctrine as the corruption and
# coexistence notices — E4 review). Confirmations keep the raw localized
# text: they carry the entry description alongside it.
_FALLBACKS = {
    _MSG_EMPTY: ("Evolved learns as you adjust — nothing stored yet. Fix "
                 "lipsync once with Kodi's audio offset slider during "
                 "playback and it will be remembered."),
    _MSG_NO_SERVICE: ("The Audio Offset Manager service is not running — "
                      "the change could not be made."),
    _MSG_UNREADABLE: ("The stored offsets file is unreadable. The service "
                      "will quarantine and reset it the next time it "
                      "starts."),
    _MSG_MUTATION_FAILED: "Could not update the stored offsets",
    _MSG_FUTURE: ("The stored offsets were saved by a newer version of "
                  "this addon. They are preserved untouched, but this "
                  "version cannot show or change them."),
}

# One presentable entry: the select label (also shown in the delete
# confirmation — it carries the value), the describe_key text (the
# deterministic sort key), and the literal store key the delete targets.
_Row = namedtuple("_Row", "label describe key")


def _noop(_message):
    return None


class ManageView:
    """Inspect + delete/clear stored offsets from the script process (P6)."""

    def __init__(self, read_entries, gui, send_mutation, *, log_debug=None):
        self._read_entries = read_entries
        self._gui = gui
        self._send_mutation = send_mutation
        self._log = log_debug or _noop

    # -- entry point ----------------------------------------------------------

    def run(self):
        """Read, render, and act on one user choice per pass until they exit."""
        heading = self._gui.localized(_HEADING)
        while True:
            try:
                entries = self._read_entries()
            except StoreUnreadable as error:
                self._log("AOMe_ManageView: store unreadable ({0})".format(error))
                # A newer-schema file is PRESERVED by the service (read-
                # only), never quarantined — its wording must not promise
                # the reset the corrupt case gets (E4 review).
                message = _MSG_FUTURE if getattr(error, 'future', False) \
                    else _MSG_UNREADABLE
                self._gui.ok(heading, self._text(message))
                return

            if not entries:
                self._log("AOMe_ManageView: store empty; nothing to manage")
                self._gui.ok(heading, self._text(_MSG_EMPTY))
                return

            rows = self._build_rows(entries)
            self._log("AOMe_ManageView: rendering {0} stored offset(s)"
                      .format(len(rows)))

            options = [row.label for row in rows]
            options.append(self._gui.localized(_LABEL_CLEAR_ALL))
            # The select dialog's built-in button says "Cancel" (skin-owned
            # wording the Python API cannot change), which misreads for a
            # view where leaving is the normal outcome — so the labeled exit
            # is a row. Both paths return; the router then reopens the
            # settings dialog the manage button closed.
            options.append(self._gui.localized(_LABEL_BACK))

            choice = self._gui.select(heading, options)
            if choice < 0 or choice == len(rows) + 1:
                return

            if choice == len(rows):
                ack = self._confirm_clear(heading)
            else:
                ack = self._confirm_delete(heading, rows[choice])

            if ack is _DECLINED:
                continue
            self._report_ack(heading, ack)
            if ack is not None and ack.get("ok") and ack.get("op") == "clear":
                # A deliberate clear: exit quietly. Looping would land on
                # the first-run education empty state, which reads as
                # "nothing was ever stored" right after the user emptied
                # the store on purpose (E4 review).
                self._log("AOMe_ManageView: store cleared; closing view")
                return

    # -- rendering ------------------------------------------------------------

    def _build_rows(self, entries):
        """Rows for every entry, sorted deterministically by display label."""
        rows = []
        for key, entry in entries.items():
            describe = self._describe(key)
            rows.append(_Row(self._label(describe, entry), describe, key))
        # (describe, key) makes the order total even if two keys ever share a
        # display label — the list must not shuffle between renders.
        rows.sort(key=lambda row: (row.describe, row.key))
        return rows

    @staticmethod
    def _describe(key):
        """describe_key with a verbatim fallback for a hand-edited key.

        A key that does not split into three segments would raise; the store
        doctrine is verbatim acceptance, so an unrecognised key is SHOWN as
        itself rather than crashing the view on a scribbled file.
        """
        try:
            return describe_key(key)
        except ValueError:
            return key

    @classmethod
    def _label(cls, describe, entry):
        delay = entry.get("delay_ms")
        sign = "+" if isinstance(delay, int) and delay > 0 else ""
        source = entry.get("source", "")
        date = cls._date_part(entry.get("updated"))
        if date:
            meta = "({0}, {1})".format(source, date)
        else:
            meta = "({0})".format(source)
        return "{0} — {1}{2} ms {3}".format(describe, sign, delay, meta)

    @staticmethod
    def _date_part(updated):
        """The date portion of an ISO ``updated`` ('2026-07-15T..' → '2026-07-15').

        Tolerates a missing or malformed value (None, non-string, empty): the
        parenthetical simply omits the date rather than crashing on a
        hand-edited file.
        """
        if not isinstance(updated, str):
            return None
        text = updated.split("T", 1)[0].strip()
        return text or None

    # -- actions --------------------------------------------------------------

    def _confirm_delete(self, heading, row):
        # The full row label, not just the profile: the confirmation must
        # show WHAT value is being deleted (field feedback on beta4).
        message = self._gui.localized(_MSG_CONFIRM_DELETE) + "\n" + row.label
        if not self._gui.yesno(heading, message):
            return _DECLINED
        self._log("AOMe_ManageView: requesting delete of {0}".format(row.key))
        return self._send_mutation("delete", row.key)

    def _confirm_clear(self, heading):
        if not self._gui.yesno(heading, self._gui.localized(_MSG_CONFIRM_CLEAR)):
            return _DECLINED
        self._log("AOMe_ManageView: requesting clear of all stored offsets")
        return self._send_mutation("clear")

    def _report_ack(self, heading, ack):
        """Surface a failed/absent ack; a success just falls through to re-read."""
        if ack is None:
            self._log("AOMe_ManageView: no ack (service not running)")
            self._gui.ok(heading, self._text(_MSG_NO_SERVICE))
            return
        if not ack.get("ok"):
            detail = ack.get("detail")
            if detail == "missing":
                # The entry was already gone (raced away by playback
                # learning or another session): the user's intent is
                # satisfied — the refreshed list is the feedback, not an
                # error dialog for a no-op (E4 review).
                self._log("AOMe_ManageView: delete target already gone")
                return
            self._log("AOMe_ManageView: mutation refused ({0})".format(detail))
            self._gui.ok(
                heading,
                self._text(_MSG_MUTATION_FAILED)
                + " (" + str(detail) + ")")
            return
        self._log("AOMe_ManageView: mutation ok ({0})".format(ack.get("detail")))

    def _text(self, string_id):
        """localized() with the English fallback for full-content dialogs."""
        return self._gui.localized(string_id) or _FALLBACKS[string_id]


# Sentinel distinguishing "user declined the confirmation" (loop, send
# nothing) from a real ack (which may itself be None on timeout). A private
# unique object so it can never collide with a channel reply.
_DECLINED = object()
