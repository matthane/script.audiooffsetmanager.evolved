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

When the store spans MORE THAN ONE HDR group, the top level renders as a
group index instead of one flat list (U0 drill-down): one single-line
row per HDR type present — display name plus entry count — with
hand-edited keys that cannot claim an HDR name (unsplittable, or a blank
hdr segment) bucketed under 'Other', sorted last (verbatim acceptance
extends to grouping: a scribbled key still lists and still deletes, and
it never gets to render a nameless group row or a blank dialog heading).
A single-group store renders the flat list — its index would be one row
of pure overhead, and the flat list IS that group's contents. The mode
derives from the GROUP count, never the entry count (DU-1 re-ruled after
the beta9 field pass: the original 8-entry threshold meant one delete
could silently dissolve the categories into a flat list whose visible
rows all shared one HDR name — field-read as being trapped inside a
category with no way back). A delete can therefore never flip the mode;
it flips only when a whole group appears or empties — a transition the
user just caused and can see.
Selecting a group lists only its entries, headed by the group name, with
the redundant HDR name dropped from the row copy ('Dolby TrueHD ·
23.976 fps'); Back from a group returns to the top level, Back from the
top level exits. The whole-store clear-all lives ONLY at the top level,
where the whole store it deletes is represented; each open group carries
its own scoped clear row instead, implemented as LOOPED SINGLE DELETES
over the channel — the P6 whitelist stays delete/clear, no batch op
exists on the wire — with a confirmation that restates the scope exactly
as the index row did ('Dolby Vision — 6 entries'). Counts include dormant rows — the index
inherits never-under-represent: every stored entry is countable there and
reachable from it. Every pass at either level re-reads the store. An open
group survives external mutations that leave it the only group (deleting
inside 'Dolby Vision' must not teleport the user into a flat list
mid-flow), while the top level re-evaluates fresh on every render; delete
confirmations always show the FULL profile line, never the shortened
in-group copy — a confirmation must not depend on which list the user
came from.

Display is toggle-aware but NEVER filtered: the injected ``per_fps`` flag
renders the 'all' segment as 'Other FPS' when the toggle is on (it is
the fallback below the exact-rate entries, not an override); when it is
off the fps axis is OMITTED from 'all' rows ('All FPS' would restate the
only semantics that mode has) and dormant per-fps rows keep their rate,
tagged '— inactive'. Every stored entry always lists — this view is the
store's only inspection surface, and clear-all's confirmation must never
under-represent what it deletes.
"""

from collections import namedtuple

from resources.lib.aom.store.keys import (HDR_DISPLAY, describe_key,
                                          describe_key_in_group, sort_key,
                                          split_key)
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
_LABEL_GROUP_ENTRY = 32135    # "{0} entry" — group-index count, singular
_LABEL_GROUP_ENTRIES = 32136  # "{0} entries" — group-index count, plural
_LABEL_OTHER_GROUP = 32137    # "Other" — the unsplittable-key bucket
_LABEL_CLEAR_GROUP = 32138    # the scoped clear row inside an open group
_MSG_CONFIRM_CLEAR_GROUP = 32139

# English fallbacks for the strings that must never render blank:
# localized() degrades to '' on a transient failure, and a blank
# information dialog teaches nothing (same doctrine as the corruption and
# coexistence notices — E4 review). Confirmations keep the raw localized
# text: they carry the entry description alongside it. The group-index
# strings are here too — 'Other' is a row's ENTIRE label (blank would
# render a nameless group), and the count templates are the only content
# beside the group name.
_FALLBACKS = {
    _MSG_EMPTY: ("Nothing is stored yet. Adjust Kodi's audio offset during "
                 "playback and the value will be saved for that stream "
                 "profile."),
    _MSG_NO_SERVICE: ("The Audio Offset Manager service is not running. "
                      "The change could not be made."),
    _MSG_UNREADABLE: ("The stored offsets file is unreadable. The service "
                      "will quarantine and reset it the next time it "
                      "starts."),
    _MSG_MUTATION_FAILED: "Could not update the stored offsets",
    _MSG_FUTURE: ("The stored offsets were saved by a newer version of "
                  "this addon. They are preserved untouched, but this "
                  "version cannot show or change them."),
    _LABEL_GROUP_ENTRY: "{0} entry",
    _LABEL_GROUP_ENTRIES: "{0} entries",
    _LABEL_OTHER_GROUP: "Other",
    _LABEL_CLEAR_GROUP: "Clear all offsets in this group",
    _MSG_CONFIRM_CLEAR_GROUP: "Delete all stored offsets in this group?",
}

# One presentable entry: the full profile line (flat rows AND the first
# line of the delete confirmation), the in-group line (drill-down rows —
# the redundant HDR name dropped, codec leading), the value/meta detail
# line, and the literal store key the delete mutation targets.
_Row = namedtuple("_Row", "describe short detail key")


def _noop(_message):
    return None


class ManageView:
    """Inspect + delete/clear stored offsets from the script process (P6)."""

    def __init__(self, read_entries, gui, send_mutation, *, per_fps=False,
                 log_debug=None):
        """``per_fps`` is the per_fps_offsets toggle at launch (it cannot
        change while the view is open — the settings dialog is closed). It
        drives DISPLAY only: 'Other FPS' vs an omitted fps axis for the
        'all' segment, and the '— inactive' tag on per-fps rows the
        lookup will not consult while the toggle is off. Never filtering:
        this view is the store's only inspection surface, so every entry
        always lists.
        """
        self._read_entries = read_entries
        self._gui = gui
        self._send_mutation = send_mutation
        self._per_fps = bool(per_fps)
        self._log = log_debug or _noop
        # The open drill-down group (an hdr segment, or _OTHER_GROUP);
        # None = top level. run() owns it; held on the instance so the
        # per-pass methods share one navigation state.
        self._group = None

    # -- entry point ----------------------------------------------------------

    def run(self):
        """Read, render, and act on one user choice per pass until they exit."""
        heading = self._gui.localized(_HEADING)
        self._group = None
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

            if self._group is not None:
                outcome = self._group_pass(heading, rows)
            else:
                # The mode question is "how many groups", never "how many
                # entries" (DU-1): a delete can empty a group, but it can
                # never silently dissolve the whole category level.
                groups = self._group_index(rows)
                if len(groups) > 1:
                    outcome = self._index_pass(heading, groups)
                else:
                    outcome = self._flat_pass(heading, rows)
            if outcome is _CLOSE:
                return

    # -- passes (one render + at most one user action each) -------------------

    def _flat_pass(self, heading, rows):
        """The single-list render: every entry as a two-line row + clear-all."""
        options = [(row.describe, row.detail) for row in rows]
        options.append(self._gui.localized(_LABEL_CLEAR_ALL))

        # Cancel/Back is the exit; the router then reopens the settings
        # dialog the manage button closed, so leaving always lands the
        # user back in settings.
        choice = self._gui.select(heading, options)
        if choice < 0:
            return _CLOSE
        if choice == len(rows):
            return self._settle(heading, self._confirm_clear(heading))
        return self._settle(heading,
                            self._confirm_delete(heading, rows[choice]))

    def _index_pass(self, heading, groups):
        """The group index: one single-line row per HDR type + clear-all.

        ``groups`` is run()'s ordered ``(segment, count)`` list — the same
        one that decided the mode. Clear-all stays on this level (and only
        this level) when grouped: its confirmation covers the whole store,
        so it belongs where the whole store is represented.
        """
        self._log("AOMe_ManageView: rendering group index ({0} group(s))"
                  .format(len(groups)))
        options = [self._group_row(segment, count)
                   for segment, count in groups]
        options.append(self._gui.localized(_LABEL_CLEAR_ALL))

        choice = self._gui.select(heading, options)
        if choice < 0:
            return _CLOSE
        if choice == len(groups):
            return self._settle(heading, self._confirm_clear(heading))
        self._group = groups[choice][0]
        self._log("AOMe_ManageView: opened group {0}"
                  .format(self._group_name(self._group)))
        return None

    def _group_pass(self, heading, rows):
        """One open group's entries; Back returns to the top level.

        The open group survives external mutations that leave it the
        only group — deleting inside a group must not teleport the user
        into a flat list mid-flow — but a group that emptied under us
        (last delete, or another session) falls back to the top level,
        which re-evaluates flat vs grouped fresh. The select is headed by
        the group name so the user always knows which drill-down they are
        in; confirmations keep the main heading and the FULL profile
        line.
        """
        group_rows = [row for row in rows
                      if self._group_of(row.key) == self._group]
        if not group_rows:
            self._log("AOMe_ManageView: open group emptied; "
                      "returning to the top level")
            self._group = None
            return None

        options = [(row.short, row.detail) for row in group_rows]
        # The scoped clear row: the whole-store clear-all stays at the top
        # level, but the set THIS row deletes is exactly the list above it.
        options.append(self._gui.localized(_LABEL_CLEAR_GROUP))
        choice = self._gui.select(self._group_name(self._group), options)
        if choice < 0:
            self._group = None
            return None
        if choice == len(group_rows):
            return self._clear_group(heading, group_rows, whole_store=(
                len(group_rows) == len(rows)))
        return self._settle(heading,
                            self._confirm_delete(heading, group_rows[choice]))

    def _clear_group(self, heading, group_rows, *, whole_store):
        """Batch-delete every entry of the open group.

        LOOPED SINGLE DELETES over the existing channel — there is no
        batch op on the wire, so the P6 whitelist (delete/clear) is
        untouched and the service side needs no change. The confirmation
        restates the scope exactly as the index row did (group name —
        count). Per-delete semantics mirror the single-delete flow: a
        'missing' ack is satisfied intent (the entry raced away) and the
        batch continues; a timeout or hard failure reports ONCE and stops
        — the re-rendered list is the truth about what remains. Clearing
        a group that was the ENTIRE store at render exits quietly like
        clear-all (looping would land on the first-run education dialog
        right after a deliberate wipe — E4 review doctrine).
        """
        message = (self._text(_MSG_CONFIRM_CLEAR_GROUP) + "\n"
                   + self._group_row(self._group, len(group_rows)))
        if not self._gui.yesno(heading, message):
            return None
        self._log("AOMe_ManageView: clearing group {0} ({1} entries)"
                  .format(self._group_name(self._group), len(group_rows)))
        for row in group_rows:
            ack = self._send_mutation("delete", row.key)
            if ack is None or (not ack.get("ok")
                               and ack.get("detail") != "missing"):
                self._report_ack(heading, ack)
                return None
        if whole_store:
            self._log("AOMe_ManageView: store cleared; closing view")
            return _CLOSE
        return None

    def _settle(self, heading, ack):
        """Post-confirmation tail shared by every pass.

        A declined confirmation just loops; a real ack is reported, and a
        deliberate clear closes the view — looping would land on the
        first-run education empty state, which reads as "nothing was ever
        stored" right after the user emptied the store on purpose (E4
        review).
        """
        if ack is _DECLINED:
            return None
        self._report_ack(heading, ack)
        if ack is not None and ack.get("ok") and ack.get("op") == "clear":
            self._log("AOMe_ManageView: store cleared; closing view")
            return _CLOSE
        return None

    # -- rendering ------------------------------------------------------------

    def _build_rows(self, entries):
        """Rows for every entry, in the grouped display order.

        ``keys.sort_key`` groups by HDR type, then codec, then rate ('all'
        first, numeric order) — total even over hand-edited keys, so the
        list never shuffles between renders.
        """
        rows = [
            _Row(self._describe(key, entry),
                 self._describe_short(key, entry),
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
        """The full profile line (flat rows, delete confirmations)."""
        return self._render_key(describe_key, key, entry)

    def _describe_short(self, key, entry):
        """The in-group row label: the HDR group name is redundant there,
        so the codec leads and the rate follows (DU-2)."""
        return self._render_key(describe_key_in_group, key, entry)

    def _render_key(self, describe_fn, key, entry):
        """One describe function + THE verbatim fallback, written once.

        A key that does not split into three segments raises; the store
        doctrine is verbatim acceptance, so an unrecognised key is SHOWN
        as itself rather than crashing the view on a scribbled file (in
        the 'Other' bucket there is no name to drop anyway). The entry's
        ``video_fps`` metadata renders the EXACT reported rate for
        per-fps keys (the truncated segment is identity, not display).
        """
        try:
            return describe_fn(key, video_fps=entry.get("video_fps"),
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

    # -- grouping -------------------------------------------------------------

    @staticmethod
    def _group_of(key):
        """The index bucket for a key: its hdr segment, or the Other bucket.

        Verbatim acceptance extends to grouping — a key that does not
        split still lists, still counts, and still deletes; it just
        cannot claim an HDR group. A splittable key with a BLANK hdr
        segment ('|all|truehd' — hand-edited; the store's writers map an
        absent HDR to the 'unknown' sentinel, never '') joins the same
        bucket: its display name would be empty, and a nameless group row
        with a blank drill-down heading represents nothing (E-review U0
        finding).
        """
        try:
            hdr = split_key(key)[0]
        except ValueError:
            return _OTHER_GROUP
        return hdr if hdr.strip() else _OTHER_GROUP

    def _group_index(self, rows):
        """Ordered ``(segment, count)`` pairs for the group index.

        Rows arrive display-sorted, so first appearance yields the same
        HDR-display order the flat list scans in; the Other bucket is
        forced last regardless of where its raw keys interleave. Counts
        include dormant rows — never-under-represent: every stored entry
        is countable from the index.
        """
        order = []
        counts = {}
        for row in rows:
            segment = self._group_of(row.key)
            if segment not in counts:
                order.append(segment)
                counts[segment] = 0
            counts[segment] += 1
        if _OTHER_GROUP in counts:
            order.remove(_OTHER_GROUP)
            order.append(_OTHER_GROUP)
        return [(segment, counts[segment]) for segment in order]

    def _group_name(self, segment):
        """Display name for a group row/heading; verbatim for a stranger."""
        if segment is _OTHER_GROUP:
            return self._text(_LABEL_OTHER_GROUP)
        return HDR_DISPLAY.get(segment, segment)

    def _group_row(self, segment, count):
        """One index row: 'Dolby Vision — 6 entries' (single-line)."""
        string_id = _LABEL_GROUP_ENTRY if count == 1 else _LABEL_GROUP_ENTRIES
        template = self._text(string_id)
        if '{' not in template:
            # A translation that dropped the placeholder would not raise —
            # format() would just return it, silently swallowing the count
            # (never-under-represent applies to the index too).
            template = _FALLBACKS[string_id]
        try:
            counted = template.format(count)
        except Exception:
            # A malformed translation degrades to the English template
            # rather than crashing the view. Deliberately broad: which
            # exception a bad template raises is the translator's choice
            # ('{0' ValueError, '{1}' IndexError, '{x}' KeyError, '{0.n}'
            # AttributeError, '{0[x]}' TypeError...).
            counted = _FALLBACKS[string_id].format(count)
        return "{0} — {1}".format(self._group_name(segment), counted)

    # -- actions --------------------------------------------------------------

    def _confirm_delete(self, heading, row):
        # Both row lines, not just the profile: the confirmation must show
        # WHAT value is being deleted (field feedback on beta4). Always the
        # FULL describe line — never the shortened in-group copy.
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
        """localized() with the English fallback for must-never-blank strings."""
        return self._gui.localized(string_id) or _FALLBACKS[string_id]


# Sentinels, private unique objects so they can never collide with real
# values: _DECLINED distinguishes "user declined the confirmation" (loop,
# send nothing) from a real ack (which may itself be None on timeout);
# _CLOSE is a pass telling run() the view is done (exit, or the deliberate
# quiet exit after a clear); _OTHER_GROUP is the index bucket for keys
# that do not split — an object, so no hdr segment string can shadow it.
_DECLINED = object()
_CLOSE = object()
_OTHER_GROUP = object()
