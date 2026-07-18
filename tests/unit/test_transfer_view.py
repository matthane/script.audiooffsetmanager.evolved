"""Unit tests for :class:`resources.lib.aome.view.transfer.TransferView`.

Pure Python through injected seams, like the manage view's suite: a
scripted ``FakeGui`` answers the dialogs (browse answers included), and a
recording ``FakeFiles`` stands in for the xbmcvfs copy/delete engine plus
the two read-only readers. The load-bearing doctrines pinned here: export
refuses garbage/empty stores and copies the file VERBATIM (the copy seam
receives a destination, never content); import's staged copy never sits
at the well-known path while a dialog waits (pre-flight stage → read →
discard in a finally, re-stage only after the confirmation, milliseconds
before the send); the view never sweeps behind a SENT request (an ack
timeout may be a merely-slow service); the confirmation states
replace-all; the only op ever sent is ``import``; and report-only on
a missing service.
"""

import pytest

from resources.lib.aome.store.offset_store import StoreUnreadable
from resources.lib.aome.view.transfer import TransferView
from tests.fakes import FakeGui


def _entry(delay_ms):
    return {"delay_ms": delay_ms, "updated": "2026-07-17T12:00:00Z",
            "source": "user"}


TWO_ENTRIES = {"dolbyvision|all|truehd": _entry(-115),
               "hdr10|all|ac3": _entry(250)}
ONE_ENTRY = {"hlg|all|eac3": _entry(-75)}


class FakeFiles:
    """The copy/delete engine + readers, with scripted outcomes.

    ``stage_ok`` may be a bool (every stage) or a list consumed per call
    (e.g. ``[True, False]`` = pre-flight copy works, the post-confirm
    re-stage fails).
    """

    def __init__(self, store_entries=None, staged_entries=None,
                 store_error=None, staged_error=None,
                 export_ok=True, stage_ok=True):
        self.store_entries = store_entries or {}
        self.staged_entries = staged_entries or {}
        self.store_error = store_error
        self.staged_error = staged_error
        self.export_ok = export_ok
        self.stage_ok = stage_ok
        self.exports = []                 # destinations handed to export_file
        self.stages = []                  # sources handed to stage_file
        self.discards = 0

    def read_entries(self):
        if self.store_error is not None:
            raise self.store_error
        return dict(self.store_entries)

    def read_staged(self):
        if self.staged_error is not None:
            raise self.staged_error
        return dict(self.staged_entries)

    def export_file(self, destination):
        self.exports.append(destination)
        return self.export_ok

    def stage_file(self, source):
        self.stages.append(source)
        if isinstance(self.stage_ok, list):
            return self.stage_ok.pop(0)
        return self.stage_ok

    def discard_staged(self):
        self.discards += 1


class FakeChannel:
    def __init__(self, acks=None):
        self.calls = []
        self._acks = list(acks or [])

    def send(self, op, key=None):
        self.calls.append((op, key))
        if self._acks:
            return self._acks.pop(0)
        return {"ok": True, "detail": "imported", "op": op, "count": 1}


def _build(files, acks=None, gui=None):
    gui = gui or FakeGui()
    channel = FakeChannel(acks=acks)
    view = TransferView(
        gui, channel.send,
        read_entries=files.read_entries,
        read_staged=files.read_staged,
        export_file=files.export_file,
        stage_file=files.stage_file,
        discard_staged=files.discard_staged,
        clock=lambda: 1752613391.0)
    return view, gui, channel


# -- export -------------------------------------------------------------------

def test_export_unreadable_store_reports_and_never_browses():
    files = FakeFiles(store_error=StoreUnreadable("invalid JSON"))
    view, gui, _ = _build(files)
    view.run_export()
    assert gui.oks == [("#32149", "#32127")]
    assert gui.browses == []
    assert files.exports == []


def test_export_future_store_gets_the_preserved_wording():
    files = FakeFiles(store_error=StoreUnreadable("newer", future=True))
    view, gui, _ = _build(files)
    view.run_export()
    assert gui.oks == [("#32149", "#32131")]
    assert files.exports == []


def test_export_empty_store_shows_education_and_never_browses():
    files = FakeFiles(store_entries={})
    view, gui, _ = _build(files)
    view.run_export()
    assert gui.oks == [("#32149", "#32122")]
    assert gui.browses == []
    assert files.exports == []


def test_export_cancelled_browse_exits_silently():
    files = FakeFiles(store_entries=TWO_ENTRIES)
    view, gui, _ = _build(files)          # no browse answers -> '' cancel
    view.run_export()
    assert gui.browses == [("folder", "#32149")]
    assert gui.oks == []
    assert files.exports == []


def test_export_copies_verbatim_to_a_timestamped_name():
    files = FakeFiles(store_entries=TWO_ENTRIES)
    gui = FakeGui()
    gui.browse_answers = ["smb://nas/backups/"]
    view, gui, _ = _build(files, gui=gui)
    view.run_export()

    assert len(files.exports) == 1
    destination = files.exports[0]
    # The copy seam receives a DESTINATION only: content never flows
    # through the view (verbatim copy is the export contract).
    assert destination.startswith("smb://nas/backups/aom-evolved-offsets-")
    assert destination.endswith(".json")
    assert "//aom-evolved" not in destination     # no doubled separator
    # The report carries the count and the full destination path.
    assert gui.oks == [("#32149", "Saved 2 entries to {0}".format(destination))]


def test_export_join_adds_a_separator_when_the_folder_lacks_one():
    files = FakeFiles(store_entries=ONE_ENTRY)
    gui = FakeGui()
    gui.browse_answers = ["D:\\backups"]
    view, gui, _ = _build(files, gui=gui)
    view.run_export()
    assert files.exports[0].startswith("D:\\backups/aom-evolved-offsets-")
    assert "1 entry" in gui.oks[0][1]     # singular count template


def test_export_report_survives_a_translation_that_dropped_a_placeholder():
    # 32153 carries TWO placeholders; a translation keeping {0} but
    # dropping {1} would format without raising and silently swallow the
    # destination path — the guard must catch the partial drop and
    # degrade to the English template.
    files = FakeFiles(store_entries=ONE_ENTRY)
    gui = FakeGui()
    gui.browse_answers = ["/backups/"]
    gui.localized_strings = {32153: "Backup {0} saved"}
    view, gui, _ = _build(files, gui=gui)
    view.run_export()
    destination = files.exports[0]
    assert gui.oks == [("#32149", "Saved 1 entry to {0}".format(destination))]


def test_export_copy_failure_is_reported():
    files = FakeFiles(store_entries=TWO_ENTRIES, export_ok=False)
    gui = FakeGui()
    gui.browse_answers = ["/backups/"]
    view, gui, _ = _build(files, gui=gui)
    view.run_export()
    assert gui.oks == [("#32149", "#32154")]


# -- import -------------------------------------------------------------------

def test_import_cancelled_browse_touches_nothing():
    files = FakeFiles()
    view, gui, channel = _build(files)    # '' cancel
    view.run_import()
    assert gui.browses == [("file", "#32151", ".json")]
    assert files.stages == []
    assert files.discards == 0
    assert channel.calls == []
    assert gui.oks == []


def test_import_discards_stale_staging_before_copying():
    # A stale staging file from a previous failure must not survive a
    # failed copy and be validated as if it were the picked file.
    files = FakeFiles(stage_ok=False)
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    view, gui, channel = _build(files, gui=gui)
    view.run_import()

    assert files.discards == 1            # the pre-copy sweep
    assert files.stages == ["/downloads/backup.json"]
    assert gui.oks == [("#32151", "#32155")]
    assert channel.calls == []


def test_import_invalid_staged_file_reports_and_discards():
    files = FakeFiles(staged_error=StoreUnreadable("unexpected shape"))
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    view, gui, channel = _build(files, gui=gui)
    view.run_import()

    assert gui.oks == [("#32151", "#32156")]
    assert files.discards == 2            # pre-copy sweep + the reject
    assert channel.calls == []


def test_import_future_staged_file_gets_its_own_wording():
    files = FakeFiles(staged_error=StoreUnreadable("newer", future=True))
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    view, gui, channel = _build(files, gui=gui)
    view.run_import()
    assert gui.oks == [("#32151", "#32157")]
    assert channel.calls == []


def test_import_empty_backup_is_refused_before_confirming():
    # "Restore nothing, deleting everything" is clear-all in a costume;
    # the real clear-all lives in the manage view and says what it does.
    files = FakeFiles(staged_entries={})
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    view, gui, channel = _build(files, gui=gui)
    view.run_import()

    assert gui.oks == [("#32151", "#32158")]
    assert gui.yesnos == []
    assert files.discards == 2
    assert channel.calls == []


def test_import_declined_confirmation_discards_and_sends_nothing():
    files = FakeFiles(staged_entries=TWO_ENTRIES)
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    gui.yesno_answers = [False]
    view, gui, channel = _build(files, gui=gui)
    view.run_import()

    # The confirmation states the count and the replace-all consequence.
    assert gui.yesnos == [("#32151",
                           "Import 2 entries? This replaces all currently "
                           "stored offsets.")]
    assert files.discards == 2
    assert channel.calls == []
    assert gui.oks == []


def test_import_confirmed_restages_sends_the_op_and_reports_the_ack_count():
    files = FakeFiles(staged_entries=TWO_ENTRIES)
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    gui.yesno_answers = [True]
    view, gui, channel = _build(
        files, gui=gui,
        acks=[{"ok": True, "detail": "imported", "op": "import", "count": 2}])
    view.run_import()

    assert channel.calls == [("import", None)]
    assert gui.oks == [("#32151", "Imported 2 entries")]
    # The pre-flight copy is discarded BEFORE the confirmation dialog and
    # the file is re-staged only after the user confirmed: pre-flight
    # stage + post-read discard + re-stage (with its own sweep).
    assert files.stages == ["/downloads/backup.json"] * 2
    assert files.discards == 3
    # After a SENT request the service owns the staging cleanup.


def test_import_restage_failure_after_confirm_is_reported():
    files = FakeFiles(staged_entries=TWO_ENTRIES, stage_ok=[True, False])
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    gui.yesno_answers = [True]
    view, gui, channel = _build(files, gui=gui)
    view.run_import()

    assert channel.calls == []            # nothing sent without a staged file
    assert gui.oks == [("#32151", "#32155")]


def test_import_malformed_ack_count_degrades_to_the_staged_count():
    files = FakeFiles(staged_entries=ONE_ENTRY)
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    gui.yesno_answers = [True]
    view, gui, channel = _build(
        files, gui=gui,
        acks=[{"ok": True, "detail": "imported", "op": "import"}])
    view.run_import()
    assert gui.oks == [("#32151", "Imported 1 entry")]


def test_import_no_ack_reports_no_service_and_never_sweeps_staging():
    files = FakeFiles(staged_entries=ONE_ENTRY)
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    gui.yesno_answers = [True]
    view, gui, channel = _build(files, gui=gui, acks=[None])
    view.run_import()

    assert gui.oks == [("#32151", "#32125")]
    # A timeout is "no reply in time", not "the request died": it may
    # still be queued behind dispatcher work, so the view must NOT delete
    # the staging file out from under it (a stale copy is inert).
    assert files.discards == 3            # only the pre-send sweeps


def test_import_refused_ack_reports_the_raw_detail_when_unmapped():
    files = FakeFiles(staged_entries=ONE_ENTRY)
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    gui.yesno_answers = [True]
    view, gui, channel = _build(
        files, gui=gui,
        acks=[{"ok": False, "detail": "read_only", "op": "import"}])
    view.run_import()
    assert gui.oks == [("#32151", "#32128 (read_only)")]
    # The service received the request; it owns the staging cleanup.
    assert files.discards == 3


@pytest.mark.parametrize("detail,message", [
    ("invalid", "#32156"),
    ("future", "#32157"),
    ("empty", "#32158"),
])
def test_import_refused_ack_maps_known_details_to_their_wordings(
        detail, message):
    # The staged file changed between the script's read and the service's
    # re-read: the refusal renders the dedicated wording, not a raw token.
    files = FakeFiles(staged_entries=ONE_ENTRY)
    gui = FakeGui()
    gui.browse_answers = ["/downloads/backup.json"]
    gui.yesno_answers = [True]
    view, gui, channel = _build(
        files, gui=gui,
        acks=[{"ok": False, "detail": detail, "op": "import"}])
    view.run_import()
    assert gui.oks == [("#32151", message)]


# -- the no-value-entry boundary ----------------------------------------------

def test_the_view_never_sends_anything_but_import():
    import inspect
    from resources.lib.aome.view import transfer

    source = inspect.getsource(transfer)
    # The only channel op this module can express is 'import' (the sibling
    # pin to the manage view's delete/clear-only rule).
    assert "_send_mutation('import')" in source
    assert "'delete'" not in source
    assert "'clear'" not in source
