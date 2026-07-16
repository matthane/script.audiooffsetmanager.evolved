"""Unit tests for :class:`resources.lib.aom.view.manage.ManageView`.

The view is pure Python driven entirely through injected seams, so these
tests use no Kodi: a scripted ``FakeGui`` (from ``tests.fakes``) answers the
dialogs, a ``FakeService`` stands in for the cross-process mutation channel
AND the single-writer store behind it (so a delete's effect shows up on the
next re-read), and plain callables/exceptions cover the reader edge cases.

The load-bearing doctrines pinned here: verbatim signed millisecond values
(no rounding, no step snapping), deterministic sort order, the empty state as
first-run education, D5 report-only on a missing service, and the P6 boundary
that the only ops this view can ever emit are ``delete`` and ``clear``.
"""

import inspect

import pytest

from resources.lib.aom.store.offset_store import StoreUnreadable
from resources.lib.aom.view.manage import ManageView
from tests.fakes import FakeGui


class FakeService:
    """Mutation channel + single-writer store stand-in.

    ``send(op, key=None)`` records the call and returns the next scripted ack
    (default: a success ack whose detail matches the op). A successful ack
    also applies the mutation to the shared entry dict, so the view's next
    re-read reflects it — exactly as the real service's single writer would.
    A ``None`` (timeout) or ``ok: False`` ack changes nothing.
    """

    def __init__(self, entries, acks=None):
        self.entries = dict(entries)
        self.calls = []
        self.reads = 0
        self._acks = list(acks or [])

    def send(self, op, key=None):
        self.calls.append((op, key))
        if self._acks:
            ack = self._acks.pop(0)
        else:
            ack = {"ok": True, "detail": "cleared" if op == "clear" else "deleted"}
        if isinstance(ack, dict) and "op" not in ack:
            # Faithful to the real handler: every ack is stamped with the
            # (whitelisted) op it answers.
            ack = dict(ack, op=op)
        if ack is not None and ack.get("ok"):
            if op == "delete":
                self.entries.pop(key, None)
            elif op == "clear":
                self.entries.clear()
        return ack

    def read(self):
        self.reads += 1
        return dict(self.entries)


def _entry(delay_ms, updated="2026-07-15T12:00:00Z", source="user"):
    return {"delay_ms": delay_ms, "updated": updated, "source": source}


# Three profiles whose display labels sort DV < HDR10 < HLG.
DV = "dolbyvision|all|truehd"
HDR10 = "hdr10|all|ac3"
HLG = "hlg|all|eac3"


def _build(entries, acks=None, gui=None):
    service = FakeService(entries, acks=acks)
    gui = gui or FakeGui()
    view = ManageView(service.read, gui, service.send)
    return view, gui, service


# -- empty / unreadable ------------------------------------------------------

def test_empty_store_shows_education_and_never_selects():
    view, gui, service = _build({})
    view.run()
    assert gui.oks == [("#32115", "#32122")]
    assert gui.selects == []
    assert service.calls == []


def test_unreadable_store_shows_notice_and_never_selects():
    def reader():
        raise StoreUnreadable("invalid JSON")

    gui = FakeGui()
    calls = []
    view = ManageView(reader, gui, lambda op, key=None: calls.append((op, key)))
    view.run()
    assert gui.oks == [("#32115", "#32127")]
    assert gui.selects == []
    assert calls == []


# -- rendering ---------------------------------------------------------------

def test_rows_render_verbatim_signed_milliseconds():
    entries = {
        DV: _entry(-115, updated="2026-07-15T12:00:00Z"),
        HDR10: _entry(9999, updated="2026-07-14T09:30:00Z"),
        HLG: _entry(-2500, updated="2026-07-13T00:00:00Z"),
    }
    view, gui, _ = _build(entries)  # no select answers -> exhausted -> -1 -> exit
    view.run()

    options = gui.selects[0][1]
    assert options[0] == "Dolby Vision | All rates | TrueHD — -115 ms (user, 2026-07-15)"
    assert options[1] == "HDR10 | All rates | AC-3 — +9999 ms (user, 2026-07-14)"
    assert options[2] == "HLG | All rates | E-AC-3 — -2500 ms (user, 2026-07-13)"
    # Verbatim: the odd values appear exactly, no rounding/step-snapping.
    assert any("+9999 ms" in opt for opt in options)
    assert any("-115 ms" in opt for opt in options)
    assert any("-2500 ms" in opt for opt in options)


def test_rows_sorted_by_label_with_clear_all_last():
    entries = {HLG: _entry(1), DV: _entry(2), HDR10: _entry(3)}
    view, gui, _ = _build(entries)
    view.run()

    options = gui.selects[0][1]
    assert options[0].startswith("Dolby Vision")
    assert options[1].startswith("HDR10")
    assert options[2].startswith("HLG")
    assert options[3] == "#32126"


def test_malformed_updated_omits_date_without_crashing():
    entries = {DV: {"delay_ms": 42, "source": "user"}}  # no 'updated'
    view, gui, _ = _build(entries)
    view.run()
    assert gui.selects[0][1][0] == "Dolby Vision | All rates | TrueHD — +42 ms (user)"


# -- navigation --------------------------------------------------------------

def test_cancel_exits_without_channel_traffic():
    entries = {DV: _entry(-115)}
    gui = FakeGui()
    gui.select_answers = [-1]
    view, gui, service = _build(entries, gui=gui)
    view.run()
    assert service.calls == []
    assert len(gui.selects) == 1


def test_delete_flow_sends_exact_key_and_re_reads():
    entries = {DV: _entry(-115), HDR10: _entry(200)}
    gui = FakeGui()
    gui.select_answers = [0, -1]     # delete the first (DV) row, then cancel
    gui.yesno_answers = [True]
    view, gui, service = _build(entries, gui=gui)
    view.run()

    assert service.calls == [("delete", DV)]
    # The store was re-read after the mutation: two renders, the second with
    # one fewer entry row (plus the clear-all row).
    assert len(gui.selects) == 2
    assert len(gui.selects[0][1]) == 3     # 2 entries + clear
    assert len(gui.selects[1][1]) == 2     # 1 entry + clear
    assert not any(opt.startswith("Dolby Vision") for opt in gui.selects[1][1])


def test_deleting_last_entry_lands_on_empty_state():
    entries = {DV: _entry(-115)}
    gui = FakeGui()
    gui.select_answers = [0]
    gui.yesno_answers = [True]
    view, gui, service = _build(entries, gui=gui)
    view.run()

    assert service.calls == [("delete", DV)]
    # Refreshed store is empty -> education dialog, then exit.
    assert gui.oks == [("#32115", "#32122")]
    assert len(gui.selects) == 1


def test_declined_delete_sends_nothing_and_loops():
    entries = {DV: _entry(-115)}
    gui = FakeGui()
    gui.select_answers = [0, -1]     # pick DV, decline, then cancel
    gui.yesno_answers = [False]
    view, gui, service = _build(entries, gui=gui)
    view.run()

    assert service.calls == []
    assert len(gui.selects) == 2     # looped back and re-rendered


def test_clear_flow_sends_clear_none_and_exits_quietly():
    entries = {DV: _entry(-115), HDR10: _entry(200)}
    gui = FakeGui()
    gui.select_answers = [2]         # the clear-all row (index == len(rows))
    gui.yesno_answers = [True]
    view, gui, service = _build(entries, gui=gui)
    view.run()

    assert service.calls == [("clear", None)]
    # E4 review: a deliberate clear exits WITHOUT the first-run education
    # empty state ("nothing stored yet" right after the user emptied the
    # store reads as data loss, not success).
    assert gui.oks == []
    assert len(gui.selects) == 1


def test_declined_clear_sends_nothing_and_loops():
    entries = {DV: _entry(-115)}
    gui = FakeGui()
    gui.select_answers = [1, -1]     # clear-all row, decline, then cancel
    gui.yesno_answers = [False]
    view, gui, service = _build(entries, gui=gui)
    view.run()

    assert service.calls == []
    assert len(gui.selects) == 2


# -- ack handling ------------------------------------------------------------

def test_ack_timeout_reports_service_not_running():
    entries = {DV: _entry(-115)}
    gui = FakeGui()
    gui.select_answers = [0, -1]     # delete, then cancel on the re-read
    gui.yesno_answers = [True]
    view, gui, service = _build(entries, acks=[None], gui=gui)
    view.run()

    assert service.calls == [("delete", DV)]
    assert ("#32115", "#32125") in gui.oks
    # None ack changed nothing: the entry survives to the next render.
    assert len(gui.selects) == 2


def test_ack_failure_reports_detail():
    entries = {DV: _entry(-115)}
    gui = FakeGui()
    gui.select_answers = [0, -1]
    gui.yesno_answers = [True]
    view, gui, service = _build(
        entries, acks=[{"ok": False, "detail": "read_only"}], gui=gui)
    view.run()

    assert service.calls == [("delete", DV)]
    heading, message = gui.oks[0]
    assert heading == "#32115"
    assert "#32128" in message
    assert "read_only" in message


# -- P6 boundary -------------------------------------------------------------

def test_constructor_exposes_no_store_writer_seam():
    params = list(inspect.signature(ManageView.__init__).parameters)
    assert params == ["self", "read_entries", "gui", "send_mutation", "log_debug"]
    # No parameter is a store writer / value setter — the view cannot write.
    for name in params:
        assert "write" not in name
        assert "store" not in name
        assert name != "set"


def test_only_delete_and_clear_ops_are_ever_emitted():
    # Walk a full scenario (delete then clear) and pin that nothing but the
    # whitelisted ops — and never a 'set' value write — reaches the channel.
    entries = {DV: _entry(-115), HDR10: _entry(200)}
    gui = FakeGui()
    gui.select_answers = [0, 1]      # delete DV, then clear-all (index 1 after)
    gui.yesno_answers = [True, True]
    view, gui, service = _build(entries, gui=gui)
    view.run()

    ops = [op for op, _ in service.calls]
    assert ops == ["delete", "clear"]
    assert all(op in ("delete", "clear") for op in ops)
    assert "set" not in ops


# -- E4 review pins ------------------------------------------------------------

def test_future_schema_store_shows_preserved_wording_not_quarantine():
    # A newer-schema file is PRESERVED by the service (read-only), never
    # quarantined: the view must not promise the corrupt case's reset.
    def reader():
        raise StoreUnreadable("newer schema version 2", future=True)

    gui = FakeGui()
    view = ManageView(reader, gui, lambda op, key=None: None)
    view.run()
    assert gui.oks == [("#32115", "#32131")]


def test_missing_delete_target_is_satisfied_silently():
    # The entry raced away (playback learning / another session): intent
    # satisfied, refreshed list is the feedback — no error dialog.
    entries = {DV: _entry(-115)}
    gui = FakeGui()
    gui.select_answers = [0]
    gui.yesno_answers = [True]
    view, gui, service = _build(
        entries, acks=[{"ok": False, "detail": "missing"}], gui=gui)
    view.run()

    assert service.calls == [("delete", DV)]
    # No failure dialog for 'missing'; the loop lands on the (unchanged)
    # store's next render — here the entry is still present because the
    # failed ack changed nothing, so the second select shows it again.
    assert all("#32128" not in message for _h, message in gui.oks)


def test_blank_localization_falls_back_to_english():
    # localized() degrades to '' on a transient failure; the full-content
    # dialogs must never render blank (same doctrine as the corruption
    # and coexistence notices).
    gui = FakeGui()
    gui.localized_strings[32122] = ''
    view, gui, service = _build({}, gui=gui)
    view.run()

    heading, message = gui.oks[0]
    assert "Evolved learns as you adjust" in message
