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
  ``localized``); ``select`` takes plain-string rows and/or
  ``(label, detail)`` tuples (two-line detail rows) and returns the chosen
  index, -1 on cancel.
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

Display is toggle-aware but NEVER filtered: the injected ``per_fps`` flag
renders the 'all' segment as 'Other FPS' when the toggle is on (it is
the fallback below the exact-rate entries, not an override) and tags
dormant per-fps rows '— inactive' when it is off. Every stored entry
always lists — this view is the store's only inspection surface, and
clear-all's confirmation must never under-represent what it deletes.
"""

from collections import namedtuple

from resources.lib.aom.store.keys import describe_key, sort_key, split_key
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

# One presentable entry: the profile line (row label AND first line of the
# delete confirmation), the value/meta detail line (second line of both),
# and the literal store key the delete mutation targets.
_Row = namedtuple("_Row", "describe detail key")


def _noop(_message):
    return None


class ManageView:
    """Inspect + delete/clear stored offsets from the script process (P6)."""

    def __init__(self, read_entries, gui, send_mutation, *, per_fps=False,
                 log_debug=None):
        """``per_fps`` is the per_fps_offsets toggle at launch (it cannot
        change while the view is open — the settings dialog is closed). It
        drives DISPLAY only: 'Other FPS' vs 'All FPS' for the 'all'
        segment, and the '— inactive' tag on per-fps rows the lookup will
        not consult while the toggle is off. Never filtering: this view is
        the store's only inspection surface, so every entry always lists.
        """
        self._read_entries = read_entries
        self._gui = gui
        self._send_mutation = send_mutation
        self._per_fps = bool(per_fps)
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

            # Entry rows are (profile, detail) tuples -> two-line detail
            # rows; the clear-all action stays a plain string row.
            options = [(row.describe, row.detail) for row in rows]
            options.append(self._gui.localized(_LABEL_CLEAR_ALL))

            # Cancel/Back is the exit; the router then reopens the settings
            # dialog the manage button closed, so leaving always lands the
            # user back in settings.
            choice = self._gui.select(heading, options)
            if choice < 0:
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
        """Rows for every entry, in the grouped display order.

        ``keys.sort_key`` groups by HDR type, then codec, then rate ('all'
        first, numeric order) — total even over hand-edited keys, so the
        list never shuffles between renders.
        """
        rows = [
            _Row(self._describe(key, entry),
                 self._detail(entry, inactive=self._is_dormant(key)),
                 key)
            for key, entry in entries.items()
        ]
        rows.sort(key=lambda row: sort_key(row.key))
        return rows

    def _is_dormant(self, key):
        """True for a per-fps entry the lookup will not consult right now.

        With the toggle off, resolution only ever reads the 'all' key, so
        an exact-rate entry is stored-but-dormant; the row is tagged rather
        than hidden (hiding would misstate clear-all's scope and strand the
        entries with no way to prune them). Unsplittable hand-edited keys
        are never tagged — nothing is known about how they resolve.
        """
        if self._per_fps:
            return False
        try:
            return split_key(key)[1] != 'all'
        except ValueError:
            return False

    def _describe(self, key, entry):
        """describe_key with a verbatim fallback for a hand-edited key.

        A key that does not split into three segments would raise; the store
        doctrine is verbatim acceptance, so an unrecognised key is SHOWN as
        itself rather than crashing the view on a scribbled file. The entry's
        ``video_fps`` metadata renders the EXACT reported rate for per-fps
        keys (the truncated segment is identity, not display).
        """
        try:
            return describe_key(key, video_fps=entry.get("video_fps"),
                                per_fps=self._per_fps)
        except ValueError:
            return key

    @staticmethod
    def _detail(entry, *, inactive):
        """The value line: '-115 ms', tagged '— inactive' when dormant.

        Just the verbatim signed value — the store's source/updated
        metadata stays in the file but out of the row (field feedback:
        it is noise at this altitude).
        """
        delay = entry.get("delay_ms")
        sign = "+" if isinstance(delay, int) and delay > 0 else ""
        detail = "{0}{1} ms".format(sign, delay)
        if inactive:
            detail += " — inactive"
        return detail

    # -- actions --------------------------------------------------------------

    def _confirm_delete(self, heading, row):
        # Both row lines, not just the profile: the confirmation must show
        # WHAT value is being deleted (field feedback on beta4).
        message = (self._gui.localized(_MSG_CONFIRM_DELETE)
                   + "\n" + row.describe + "\n" + row.detail)
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
