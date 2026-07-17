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


def _build(entries, acks=None, gui=None, per_fps=False):
    service = FakeService(entries, acks=acks)
    gui = gui or FakeGui()
    view = ManageView(service.read, gui, service.send, per_fps=per_fps)
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
    # Single HDR type -> flat list (multi-type stores render the group
    # index; flat rendering is pinned on the store shape that shows it).
    entries = {
        DV: _entry(-115, updated="2026-07-15T12:00:00Z"),
        "dolbyvision|all|ac3": _entry(9999, updated="2026-07-14T09:30:00Z"),
        "dolbyvision|all|eac3": _entry(-2500, updated="2026-07-13T00:00:00Z"),
    }
    view, gui, _ = _build(entries)  # no select answers -> exhausted -> -1 -> exit
    view.run()

    options = gui.selects[0][1]
    assert options[0] == ("Dolby Vision | All FPS | Dolby Digital", "+9999 ms")
    assert options[1] == ("Dolby Vision | All FPS | Dolby Digital Plus",
                          "-2500 ms")
    assert options[2] == ("Dolby Vision | All FPS | Dolby TrueHD", "-115 ms")
    # Verbatim: the odd values appear exactly, no rounding/step-snapping.
    details = [detail for _profile, detail in options[:3]]
    assert any("+9999 ms" in detail for detail in details)
    assert any("-115 ms" in detail for detail in details)
    assert any("-2500 ms" in detail for detail in details)


def test_index_rows_sorted_by_hdr_label_with_clear_all_last():
    # A multi-type store renders the group index, ordered by HDR display
    # name with clear-all last as a plain string row.
    entries = {HLG: _entry(1), DV: _entry(2), HDR10: _entry(3)}
    gui = _grouped_gui()
    view, gui, _ = _build(entries, gui=gui)
    view.run()

    options = gui.selects[0][1]
    assert options == [
        "Dolby Vision — 1 entry",
        "HDR10 — 1 entry",
        "HLG — 1 entry",
        "#32126",
    ]


def test_per_fps_rows_show_the_exact_reported_rate():
    # E7 beta4 field feedback: a per-fps row must show the rate the user
    # recognises (23.976), not the truncated key identity (23). The exact
    # rate is the entry's video_fps metadata; entries without it (hand-
    # edited) degrade to the segment.
    entries = {
        "dolbyvision|23|eac3": dict(_entry(-25), video_fps=23.976),
        "dolbyvision|59|ac3": _entry(75),          # no metadata -> segment
    }
    view, gui, _ = _build(entries, per_fps=True)
    view.run()

    options = gui.selects[0][1]
    assert options[0] == ("Dolby Vision | 59 fps | Dolby Digital", "+75 ms")
    assert options[1] == ("Dolby Vision | 23.976 fps | Dolby Digital Plus",
                          "-25 ms")


def test_toggle_off_tags_per_fps_rows_inactive_and_never_hides():
    # With per_fps off the lookup only reads 'all' keys: exact-rate entries
    # are stored-but-dormant. They TAG rather than hide (this view is the
    # store's only inspection surface; clear-all must not under-represent
    # its scope), and the 'all' label stays literally true: 'All FPS'.
    entries = {
        "dolbyvision|all|eac3": _entry(-25),
        "dolbyvision|23|eac3": dict(_entry(125), video_fps=23.976),
    }
    view, gui, _ = _build(entries, per_fps=False)
    view.run()

    options = gui.selects[0][1]
    assert options[0] == ("Dolby Vision | All FPS | Dolby Digital Plus",
                          "-25 ms")
    assert options[1] == ("Dolby Vision | 23.976 fps | Dolby Digital Plus",
                          "+125 ms — inactive")
    assert len(options) == 3               # both entries + clear-all


def test_toggle_on_renders_all_as_other_rates_with_no_inactive_tags():
    # Under the toggle the 'all' entry is the fallback BELOW the exact
    # entries (exact -> all -> miss), so 'All FPS' would misread as an
    # override: it renders 'Other FPS'. Nothing is dormant when on.
    entries = {
        "dolbyvision|all|eac3": _entry(-25),
        "dolbyvision|23|eac3": dict(_entry(125), video_fps=23.976),
    }
    view, gui, _ = _build(entries, per_fps=True)
    view.run()

    options = gui.selects[0][1]
    assert options[0] == ("Dolby Vision | Other FPS | Dolby Digital Plus",
                          "-25 ms")
    assert options[1] == ("Dolby Vision | 23.976 fps | Dolby Digital Plus",
                          "+125 ms")
    assert not any(isinstance(opt, tuple) and "inactive" in opt[1]
                   for opt in options)


def test_rows_group_by_codec_then_numeric_rate():
    # The tuned display order within one HDR mode: codecs alphabetical,
    # and each codec's 'All FPS' entry before its per-fps entries in
    # NUMERIC rate order (119 after 23, not before).
    entries = {
        "dolbyvision|119|eac3": dict(_entry(1), video_fps=119.88),
        "dolbyvision|23|eac3": dict(_entry(2), video_fps=23.976),
        "dolbyvision|all|eac3": _entry(3),
        "dolbyvision|24|truehd": dict(_entry(4), video_fps=24.0),
        "dolbyvision|all|ac3": _entry(5),
    }
    view, gui, _ = _build(entries)
    view.run()

    profiles = [opt[0] for opt in gui.selects[0][1][:-1]]
    assert profiles == [
        "Dolby Vision | All FPS | Dolby Digital",
        "Dolby Vision | All FPS | Dolby Digital Plus",
        "Dolby Vision | 23.976 fps | Dolby Digital Plus",
        "Dolby Vision | 119.88 fps | Dolby Digital Plus",
        "Dolby Vision | 24 fps | Dolby TrueHD",
    ]


def test_bare_entry_renders_without_meta_fields():
    # source/updated stay in the store file but out of the row (field
    # feedback: noise at this altitude) — an entry carrying only the value
    # renders identically to a full one.
    entries = {DV: {"delay_ms": 42}}
    view, gui, _ = _build(entries)
    view.run()
    assert gui.selects[0][1][0] == ("Dolby Vision | All FPS | Dolby TrueHD",
                                    "+42 ms")


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
    entries = {DV: _entry(-115), "dolbyvision|all|ac3": _entry(200)}
    gui = FakeGui()
    gui.select_answers = [1, -1]     # delete the TrueHD row, then cancel
    gui.yesno_answers = [True]
    view, gui, service = _build(entries, gui=gui)
    view.run()

    assert service.calls == [("delete", DV)]
    # The store was re-read after the mutation: two renders, the second with
    # one fewer entry row (plus the clear-all row).
    assert len(gui.selects) == 2
    assert len(gui.selects[0][1]) == 3     # 2 entries + clear
    assert len(gui.selects[1][1]) == 2     # 1 entry + clear
    assert not any(isinstance(opt, tuple) and "TrueHD" in opt[0]
                   for opt in gui.selects[1][1])


def test_delete_confirmation_shows_the_stored_value():
    # Field feedback (beta4): the confirmation must show WHAT is being
    # deleted — the full row label with the value, not just the profile.
    entries = {DV: _entry(-115)}
    gui = FakeGui()
    gui.select_answers = [0]
    gui.yesno_answers = [False]      # just inspect the confirmation
    view, gui, service = _build(entries, gui=gui)
    view.run()

    heading, message = gui.yesnos[0]
    assert heading == "#32115"
    assert "-115 ms" in message
    assert "Dolby Vision | All FPS | Dolby TrueHD" in message


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
    entries = {DV: _entry(-115), "dolbyvision|all|ac3": _entry(200)}
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
    assert params == ["self", "read_entries", "gui", "send_mutation",
                      "per_fps", "log_debug"]
    # No parameter is a store writer / value setter — the view cannot write.
    for name in params:
        assert "write" not in name
        assert "store" not in name
        assert name != "set"


def test_only_delete_and_clear_ops_are_ever_emitted():
    # Walk a full scenario (delete then clear) and pin that nothing but the
    # whitelisted ops — and never a 'set' value write — reaches the channel.
    entries = {DV: _entry(-115), "dolbyvision|all|ac3": _entry(200)}
    gui = FakeGui()
    gui.select_answers = [0, 1]      # delete a row, then clear-all (index 1 after)
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
    assert "Nothing is stored yet" in message


# -- U0 grouped drill-down ----------------------------------------------------
#
# When the store spans 2+ HDR groups the top level is a group index; a group
# opens into its entries with the redundant HDR name dropped from the row
# copy (DU-2), Back returns to the index, and clear-all lives only at the
# top level. A single-group store renders the flat list (DU-1 re-ruled after
# the beta9 field pass: mode comes from the GROUP count, never the entry
# count, so a delete can never silently dissolve the categories). These
# tests give the count templates and the 'Other' label real translations so
# the labels read as they would on screen.

_COUNT_OVERRIDES = {32135: "{0} entry", 32136: "{0} entries", 32137: "Other"}


def _grouped_gui():
    gui = FakeGui()
    gui.localized_strings.update(_COUNT_OVERRIDES)
    return gui


def _grouped_entries():
    # Four real HDR groups plus one hand-scribbled unsplittable key (the
    # 'Other' bucket) -> multi-group store, renders the index.
    return {
        "dolbyvision|all|truehd": _entry(1),
        "dolbyvision|23|truehd": dict(_entry(2), video_fps=23.976),
        "dolbyvision|all|eac3": _entry(3),
        "hdr10|all|ac3": _entry(4),
        "hdr10|59|ac3": dict(_entry(5), video_fps=59.94),
        "hdr10|all|dtshd_ma": _entry(6),
        "hlg|all|aac": _entry(7),
        "hlg|all|opus": _entry(8),
        "sdr|all|flac": _entry(9),
        "scribbled-key": _entry(10),
    }


def test_multi_group_store_renders_group_index_with_counts():
    view, gui, service = _build(_grouped_entries(), gui=_grouped_gui())
    view.run()

    heading, options = gui.selects[0]
    assert heading == "#32115"
    # HDR-display order, 'Other' forced last (its raw key would otherwise
    # interleave: 'scribbled-key' sorts before 'sdr'), clear-all closing.
    assert options == [
        "Dolby Vision — 3 entries",
        "HDR10 — 3 entries",
        "HLG — 2 entries",
        "SDR — 1 entry",
        "Other — 1 entry",
        "#32126",
    ]
    # The index is single-line rows only — no detail tuples.
    assert all(isinstance(option, str) for option in options)
    assert service.calls == []


def test_single_group_renders_flat_and_second_group_flips_to_index():
    # DU-1 (re-ruled): flat vs grouped is a function of the GROUP count
    # only. A single-group store — however large — stays flat (its index
    # would be one row of pure overhead)...
    single = {"hdr10|{0}|ac3".format(i): _entry(i) for i in range(1, 10)}
    view, gui, _ = _build(single, per_fps=True)
    view.run()
    assert len(gui.selects[0][1]) == 10          # 9 rows + clear-all
    assert all(isinstance(option, tuple)
               for option in gui.selects[0][1][:-1])

    # ...and a second HDR type — however small the store — flips to the
    # index.
    two_groups = {"hdr10|all|ac3": _entry(1), DV: _entry(2)}
    view, gui, _ = _build(two_groups, gui=_grouped_gui())
    view.run()
    assert gui.selects[0][1] == ["Dolby Vision — 1 entry", "HDR10 — 1 entry",
                                 "#32126"]


def test_group_drilldown_shows_short_rows_and_back_returns_to_index():
    gui = _grouped_gui()
    gui.select_answers = [0, -1, -1]     # open DV, back to index, exit
    view, gui, service = _build(_grouped_entries(), gui=gui, per_fps=True)
    view.run()

    assert len(gui.selects) == 3
    # The drill-down is headed by the group name and lists ONLY its
    # entries — HDR name dropped, codec leading (DU-2) — plus the scoped
    # group-clear row; the whole-store clear-all row stays at the top
    # level only.
    heading, options = gui.selects[1]
    assert heading == "Dolby Vision"
    assert options == [
        ("Dolby Digital Plus · Other FPS", "+3 ms"),
        ("Dolby TrueHD · Other FPS", "+1 ms"),
        ("Dolby TrueHD · 23.976 fps", "+2 ms"),
        "#32138",
    ]
    assert "#32126" not in options
    # Back from the group re-rendered the index; Back from the index
    # exited without any channel traffic. Every pass re-read the store.
    assert gui.selects[2][1] == gui.selects[0][1]
    assert service.calls == []
    assert service.reads == 3


def test_other_bucket_lists_unsplittable_key_verbatim():
    gui = _grouped_gui()
    gui.select_answers = [4, -1, -1]     # open the Other bucket
    view, gui, _ = _build(_grouped_entries(), gui=gui)
    view.run()

    heading, options = gui.selects[1]
    assert heading == "Other"
    # Verbatim acceptance: the scribbled key shows as itself (there is no
    # HDR name to drop), value verbatim, never a crash.
    assert options == [("scribbled-key", "+10 ms"), "#32138"]


def test_group_delete_confirms_with_full_profile_line_and_stays_in_group():
    gui = _grouped_gui()
    gui.select_answers = [0, 0, -1, -1]  # open DV, delete a row, back, exit
    gui.yesno_answers = [True]
    view, gui, service = _build(_grouped_entries(), gui=gui, per_fps=True)
    view.run()

    # The confirmation keeps the main heading and the FULL profile line —
    # never the shortened in-group copy (what is deleted must not depend
    # on which list the user came from).
    heading, message = gui.yesnos[0]
    assert heading == "#32115"
    assert "Dolby Vision | Other FPS | Dolby Digital Plus" in message
    assert "+3 ms" in message
    assert service.calls == [("delete", "dolbyvision|all|eac3")]
    # After the delete the (re-read) group re-rendered with 2 rows (+ the
    # group-clear row).
    assert len(gui.selects) == 4
    assert gui.selects[2][0] == "Dolby Vision"
    assert len(gui.selects[2][1]) == 3


def test_deleting_a_groups_last_entry_returns_to_index_without_it():
    gui = _grouped_gui()
    gui.select_answers = [3, 0, -1]      # open SDR, delete its only row, exit
    gui.yesno_answers = [True]
    view, gui, service = _build(_grouped_entries(), gui=gui)
    view.run()

    assert service.calls == [("delete", "sdr|all|flac")]
    # The emptied group falls back to the index (9 entries: still
    # grouped), its row gone, everything else intact.
    final_options = gui.selects[-1][1]
    assert not any(isinstance(option, str) and option.startswith("SDR")
                   for option in final_options)
    assert "Other — 1 entry" in final_options


def test_store_emptied_under_an_open_group_lands_on_empty_state():
    service = FakeService(_grouped_entries())
    gui = _grouped_gui()
    gui.select_answers = [0]             # open DV...
    original_select = gui.select

    def select_then_wipe(heading, options):
        choice = original_select(heading, options)
        service.entries.clear()          # ...but another session clears
        return choice

    gui.select = select_then_wipe
    view = ManageView(service.read, gui, service.send)
    view.run()

    # The next pass reads the empty store before rendering the group:
    # education dialog, exit — never a stale group render.
    assert gui.oks == [("#32115", "#32122")]
    assert len(gui.selects) == 1


def test_reread_per_pass_reflects_external_mutations():
    service = FakeService(_grouped_entries())
    gui = _grouped_gui()
    gui.select_answers = [0, -1, -1]     # open DV, back, exit
    original_select = gui.select
    state = {"calls": 0}

    def select_with_racing_delete(heading, options):
        state["calls"] += 1
        if state["calls"] == 1:
            # A DV entry raced away while the user was choosing a group.
            service.entries.pop("dolbyvision|all|eac3")
        return original_select(heading, options)

    gui.select = select_with_racing_delete
    view = ManageView(service.read, gui, service.send, per_fps=True)
    view.run()

    # The drill-down read fresh: the raced-away row is already gone
    # (2 rows + the group-clear row).
    assert len(gui.selects[1][1]) == 3
    # And the re-rendered index shows the new count.
    assert "Dolby Vision — 2 entries" in gui.selects[2][1]


def test_dormant_rows_count_and_tag_at_both_levels():
    gui = _grouped_gui()
    gui.select_answers = [0, -1, -1]
    view, gui, _ = _build(_grouped_entries(), gui=gui, per_fps=False)
    view.run()

    # The index counts the dormant 23-fps row (never-under-represent:
    # every stored entry is countable from the index).
    assert "Dolby Vision — 3 entries" in gui.selects[0][1]
    # And the drill-down tags it exactly as the flat list would.
    options = gui.selects[1][1]
    assert ("Dolby TrueHD · 23.976 fps", "+2 ms — inactive") in options
    assert ("Dolby TrueHD · All FPS", "+1 ms") in options


def test_clear_from_group_index_exits_quietly():
    gui = _grouped_gui()
    gui.select_answers = [5]             # the clear-all row, after 5 groups
    gui.yesno_answers = [True]
    view, gui, service = _build(_grouped_entries(), gui=gui)
    view.run()

    assert service.calls == [("clear", None)]
    assert gui.oks == []                 # quiet exit, no education dialog
    assert len(gui.selects) == 1


def test_deletes_never_flip_the_mode_until_a_group_empties():
    # The beta9 field find, pinned as the DU-1 re-ruling: deleting entries
    # NEVER dissolves the category level while 2+ groups remain; the flat
    # list appears only when the store is down to a single group — a
    # transition the user just caused (they emptied a category) and can
    # see.
    entries = {"hdr10|{0}|ac3".format(i): _entry(i) for i in range(1, 9)}
    entries["dolbyvision|all|truehd"] = _entry(9)
    gui = _grouped_gui()
    # open HDR10, delete one, back — then open DV, delete its only entry,
    # exit from the flat list that (comprehensibly) remains.
    gui.select_answers = [1, 0, -1, 0, 0, -1]
    gui.yesno_answers = [True, True]
    view, gui, service = _build(entries, gui=gui, per_fps=True)
    view.run()

    assert len(service.calls) == 2
    # Deleting inside HDR10 (9 -> 8 entries, the exact field scenario):
    # the group re-renders (7 rows + group-clear row), and Back lands on
    # the INDEX, not a flat list.
    assert gui.selects[2][0] == "HDR10"
    assert len(gui.selects[2][1]) == 8
    assert gui.selects[3][1] == ["Dolby Vision — 1 entry",
                                 "HDR10 — 7 entries", "#32126"]
    # Emptying the DV group leaves one group: NOW the top level is flat —
    # the single remaining group's contents.
    assert gui.selects[4][0] == "Dolby Vision"
    assert len(gui.selects[5][1]) == 8   # 7 rows + clear-all
    assert all(isinstance(option, tuple)
               for option in gui.selects[5][1][:-1])


# -- U0 review pins ------------------------------------------------------------

def test_group_declined_delete_sends_nothing_and_stays_in_group():
    # The flat path's declined-loops pin, re-pinned for the drill-down:
    # declining must keep the user IN the open group (a regression that
    # cleared the open group would silently teleport them to the index).
    gui = _grouped_gui()
    gui.select_answers = [0, 0, -1, -1]  # open DV, pick a row, back, exit
    gui.yesno_answers = [False]
    view, gui, service = _build(_grouped_entries(), gui=gui, per_fps=True)
    view.run()

    assert service.calls == []
    assert len(gui.selects) == 4
    assert gui.selects[2][0] == "Dolby Vision"      # same group, re-rendered
    assert gui.selects[2][1] == gui.selects[1][1]


def test_group_delete_failed_ack_reports_under_main_heading_and_stays():
    # The flat path's ack-failure pin, re-pinned for the drill-down: the
    # failure dialog carries the MAIN heading (not the group name) and the
    # user stays in the open group with the entry still listed.
    gui = _grouped_gui()
    gui.select_answers = [0, 0, -1, -1]
    gui.yesno_answers = [True]
    view, gui, service = _build(
        _grouped_entries(), acks=[{"ok": False, "detail": "read_only"}],
        gui=gui, per_fps=True)
    view.run()

    assert service.calls == [("delete", "dolbyvision|all|eac3")]
    heading, message = gui.oks[0]
    assert heading == "#32115"
    assert "#32128" in message and "read_only" in message
    # Failed ack changed nothing: still in the group, all 3 rows present
    # (+ the group-clear row).
    assert gui.selects[2][0] == "Dolby Vision"
    assert len(gui.selects[2][1]) == 4


def test_group_delete_ack_timeout_reports_service_missing_and_stays():
    gui = _grouped_gui()
    gui.select_answers = [0, 0, -1, -1]
    gui.yesno_answers = [True]
    view, gui, service = _build(_grouped_entries(), acks=[None], gui=gui,
                                per_fps=True)
    view.run()

    assert ("#32115", "#32125") in gui.oks
    assert gui.selects[2][0] == "Dolby Vision"
    assert len(gui.selects[2][1]) == 4


def test_blank_hdr_segment_key_joins_the_other_bucket():
    # '|all|truehd' splits fine (three parts, blank hdr), so it would
    # otherwise form a NAMELESS group sorted first, with '' as the
    # drill-down heading. It belongs in 'Other' with the unsplittable
    # keys: no blank row, no blank heading, still fully deletable.
    entries = {"hdr10|{0}|ac3".format(i): _entry(i) for i in range(1, 9)}
    entries["|all|truehd"] = _entry(9)
    entries["scribbled-key"] = _entry(10)
    gui = _grouped_gui()
    gui.select_answers = [1, -1, -1]     # open Other, back, exit
    view, gui, _ = _build(entries, gui=gui, per_fps=True)
    view.run()

    assert gui.selects[0][1] == ["HDR10 — 8 entries", "Other — 2 entries",
                                 "#32126"]
    heading, options = gui.selects[1]
    assert heading == "Other"
    assert options == [
        ("Dolby TrueHD · Other FPS", "+9 ms"),   # blank hdr, audio intact
        ("scribbled-key", "+10 ms"),
        "#32138",
    ]


# -- per-group clear -----------------------------------------------------------
#
# The scoped clear row inside an open group: looped single deletes over the
# existing channel — no batch op exists on the wire, the P6 whitelist stays
# delete/clear. Confirmation restates the scope as the index row shows it.

def test_group_clear_confirmation_shows_scope_and_decline_sends_nothing():
    gui = _grouped_gui()
    gui.select_answers = [0, 3, -1, -1]  # open DV, its clear row, back, exit
    gui.yesno_answers = [False]
    view, gui, service = _build(_grouped_entries(), gui=gui, per_fps=True)
    view.run()

    heading, message = gui.yesnos[0]
    assert heading == "#32115"
    assert "#32139" in message
    # The scope line is the index row's exact copy: name — count.
    assert "Dolby Vision — 3 entries" in message
    assert service.calls == []
    # Declined: still in the group, re-rendered.
    assert gui.selects[2][0] == "Dolby Vision"


def test_group_clear_deletes_every_group_key_and_lands_on_index():
    gui = _grouped_gui()
    gui.select_answers = [0, 3, -1]      # open DV, clear the group, exit
    gui.yesno_answers = [True]
    view, gui, service = _build(_grouped_entries(), gui=gui, per_fps=True)
    view.run()

    # One delete per group entry, exact keys, display order — and nothing
    # but deletes on the wire (P6: no batch op exists).
    assert service.calls == [
        ("delete", "dolbyvision|all|eac3"),
        ("delete", "dolbyvision|all|truehd"),
        ("delete", "dolbyvision|23|truehd"),
    ]
    # The emptied group falls back to the index: DV gone, others intact,
    # no error/education dialog.
    assert gui.oks == []
    final_options = gui.selects[-1][1]
    assert not any(isinstance(option, str) and option.startswith("Dolby Vision")
                   for option in final_options)
    assert "HDR10 — 3 entries" in final_options


def test_group_clear_of_the_entire_store_exits_quietly():
    # The open group can BE the whole store (another session shrank the
    # rest away): clearing it then mirrors clear-all's quiet exit — no
    # first-run education dialog right after a deliberate wipe.
    entries = {
        "dolbyvision|all|truehd": _entry(1),
        "dolbyvision|all|eac3": _entry(2),
        "hdr10|all|ac3": _entry(3),
    }
    service = FakeService(entries)
    gui = _grouped_gui()
    gui.select_answers = [0, 2]          # open DV, then its clear row
    gui.yesno_answers = [True]
    original_select = gui.select

    def select_then_shrink(heading, options):
        choice = original_select(heading, options)
        # After the index selection, the other group races away.
        if len(gui.selects) == 1:
            service.entries.pop("hdr10|all|ac3")
        return choice

    gui.select = select_then_shrink
    view = ManageView(service.read, gui, service.send)
    view.run()

    assert service.calls == [
        ("delete", "dolbyvision|all|eac3"),
        ("delete", "dolbyvision|all|truehd"),
    ]
    assert service.entries == {}
    # Quiet exit: no education dialog, no further renders.
    assert gui.oks == []
    assert len(gui.selects) == 2


def test_group_clear_stops_on_hard_failure_and_reports_once():
    gui = _grouped_gui()
    # open DV, clear the group; the second delete is refused -> one
    # failure dialog, batch stops, back in the group with the remainder.
    gui.select_answers = [0, 3, -1, -1]
    gui.yesno_answers = [True]
    view, gui, service = _build(
        _grouped_entries(),
        acks=[{"ok": True, "detail": "deleted"},
              {"ok": False, "detail": "read_only"}],
        gui=gui, per_fps=True)
    view.run()

    assert len(service.calls) == 2       # third delete never attempted
    failure_dialogs = [m for _h, m in gui.oks if "#32128" in m]
    assert len(failure_dialogs) == 1
    assert "read_only" in failure_dialogs[0]
    # The re-rendered group holds the two survivors (+ clear row).
    assert gui.selects[2][0] == "Dolby Vision"
    assert len(gui.selects[2][1]) == 3


def test_group_clear_timeout_reports_service_missing_and_stops():
    gui = _grouped_gui()
    gui.select_answers = [0, 3, -1, -1]
    gui.yesno_answers = [True]
    view, gui, service = _build(_grouped_entries(), acks=[None], gui=gui,
                                per_fps=True)
    view.run()

    assert len(service.calls) == 1       # D5 report-only: stop immediately
    assert ("#32115", "#32125") in gui.oks
    # Nothing was deleted: the group re-renders with all 3 rows.
    assert len(gui.selects[2][1]) == 4


def test_group_clear_missing_acks_are_satisfied_and_continue():
    # Every entry raced away between render and delete: each 'missing'
    # ack is satisfied intent — the batch continues silently, exactly
    # like the single-delete flow.
    gui = _grouped_gui()
    gui.select_answers = [0, 3, -1, -1]
    gui.yesno_answers = [True]
    missing = {"ok": False, "detail": "missing"}
    view, gui, service = _build(
        _grouped_entries(), acks=[dict(missing), dict(missing), dict(missing)],
        gui=gui, per_fps=True)
    view.run()

    assert len(service.calls) == 3       # all attempted, none aborted
    assert gui.oks == []                 # and no error dialog for no-ops


def test_count_template_degrades_on_malformed_or_placeholderless_translation():
    # '{0.n}' raises AttributeError, '{0[x]}' TypeError — outside the
    # original (IndexError, KeyError, ValueError) guard — and a template
    # with NO placeholder never raises at all, silently dropping the
    # count. All three must degrade to the English template, never crash.
    for bad in ("{0.n} entries", "{0[x]} entries", "entries", "{0"):
        gui = _grouped_gui()
        gui.localized_strings[32136] = bad
        view, gui, _ = _build(_grouped_entries(), gui=gui)
        view.run()
        assert "Dolby Vision — 3 entries" in gui.selects[0][1], bad
