"""Unit tests for :class:`resources.lib.aom.view.logexport.LogExportView`.

Pure Python through injected seams, like the transfer view's suite: a
scripted ``FakeGui`` answers the dialogs, list-of-lines readers stand in
for the log streams, and a recording writer captures the rendered file.
The load-bearing doctrines pinned here: filtering is per ENTRY (a
timestamped line plus its continuations — tracebacks export whole);
the addon id catches prefix-less traceback/lifecycle entries; the header
whitelist is trusted only near the top of a file; the export refuses to
write when nothing the addon logged is present (header-only is not a
report); redaction folds resolved roots to special:// longest-first and
masks URL credentials; the size cap drops whole entries OLDEST-first and
announces the trim; and a reader failing mid-stream surrenders what it
produced, not the whole export.
"""

import pytest

from resources.lib.aom.view import logexport
from resources.lib.aom.view.logexport import LogExportView
from tests.fakes import FakeGui


STAMP = 1752613391.0  # fixed clock for deterministic filenames

HEADER_LINES = [
    "2026-07-18 10:00:00.100 T:1 info <general>: Starting Kodi (22.0-beta1). "
    "Platform: Windows NT x86 64-bit",
    "2026-07-18 10:00:00.101 T:1 info <general>: Running on Windows 11, "
    "kernel: Windows NT x86 64-bit",
]

AOME_LINE = ("2026-07-18 10:01:00.000 T:7 info <general>: "
             "AOMe_StreamDetector: probed dolbyvision|all|truehd")

NOISE_LINE = ("2026-07-18 10:01:01.000 T:7 info <general>: "
              "CApplication: some unrelated chatter about a video")

TRACEBACK_ENTRY = [
    "2026-07-18 10:02:00.000 T:7 error <general>: EXCEPTION Thrown "
    "(PythonToCppException): ...",
    "Traceback (most recent call last):",
    '  File "C:\\Kodi\\home\\addons\\script.audiooffsetmanager.evolved\\'
    'resources\\lib\\aom\\view\\manage.py", line 1, in run',
    "ValueError: boom",
]


class FakeWriter:
    def __init__(self, ok=True):
        self.ok = ok
        self.writes = []                 # (destination, text)

    def __call__(self, destination, text):
        self.writes.append((destination, text))
        return self.ok


def _build(old=None, current=None, writer=None, gui=None, redactions=(),
           version='1.0.0~beta13'):
    gui = gui or FakeGui()
    writer = writer or FakeWriter()
    view = LogExportView(
        gui,
        read_old_log=lambda: old,
        read_current_log=lambda: current,
        write_export=writer,
        redactions=redactions,
        version=version,
        clock=lambda: STAMP)
    return view, gui, writer


def _exported(writer):
    assert len(writer.writes) == 1
    return writer.writes[0]


# -- guards -------------------------------------------------------------------

def test_no_readable_log_reports_and_never_browses():
    view, gui, writer = _build(old=None, current=None)
    view.run_export()
    assert gui.oks == [("#32161", "#32166")]
    assert gui.browses == []
    assert writer.writes == []


def test_header_only_logs_refuse_to_export():
    # Kodi's startup lines match the whitelist, but nothing the addon
    # logged is present: writing a shell teaches nothing — the dialog
    # explains the flow instead.
    view, gui, writer = _build(current=HEADER_LINES + [NOISE_LINE])
    view.run_export()
    assert gui.oks == [("#32161", "#32163")]
    assert gui.browses == []
    assert writer.writes == []


def test_cancelled_browse_writes_nothing():
    view, gui, writer = _build(current=[AOME_LINE])
    view.run_export()
    assert gui.browses == [('folder', "#32161")]
    assert writer.writes == []
    assert gui.oks == []


def test_write_failure_reports():
    writer = FakeWriter(ok=False)
    gui = FakeGui()
    gui.browse_answers = ['/reports/']
    view, gui, writer = _build(current=[AOME_LINE], writer=writer, gui=gui)
    view.run_export()
    assert gui.oks == [("#32161", "#32165")]


# -- filtering ----------------------------------------------------------------

def _run_success(old=None, current=None, redactions=(), version=''):
    gui = FakeGui()
    gui.browse_answers = ['/reports/']
    view, gui, writer = _build(old=old, current=current, gui=gui,
                               redactions=redactions, version=version)
    view.run_export()
    return _exported(writer), gui


def test_export_keeps_addon_and_header_entries_drops_noise():
    (destination, text), gui = _run_success(
        current=HEADER_LINES + [NOISE_LINE, AOME_LINE])
    assert "Starting Kodi" in text
    assert "Running on Windows 11" in text
    assert "AOMe_StreamDetector" in text
    assert "unrelated chatter" not in text
    stamp = logexport.time.strftime(
        '%Y%m%d-%H%M%S', logexport.time.localtime(STAMP))
    assert destination == '/reports/aome-log-{0}.log'.format(stamp)
    # The success dialog names the destination via the guarded template.
    assert gui.oks == [("#32161", "Saved the filtered log to {0}".format(
        destination))]


def test_traceback_entry_exports_whole_via_the_addon_id():
    # No AOMe prefix anywhere in the entry: the addon id in the frame
    # path is what keeps it, and the continuation lines ride along.
    (_dest, text), _gui = _run_success(
        current=[NOISE_LINE] + TRACEBACK_ENTRY + [NOISE_LINE])
    assert "EXCEPTION Thrown" in text
    assert "Traceback (most recent call last):" in text
    assert "ValueError: boom" in text
    assert "unrelated chatter" not in text


def test_header_whitelist_is_not_trusted_deep_in_the_file():
    # The same "Starting Kodi" text past the scan window must not ride
    # along — only real startup blocks are wanted.
    deep = [NOISE_LINE] * logexport._HEADER_SCAN_LINES
    (_dest, text), _gui = _run_success(
        current=deep + [HEADER_LINES[0], AOME_LINE])
    assert "Starting Kodi" not in text
    assert "AOMe_StreamDetector" in text


def test_headless_leading_lines_attach_to_a_first_entry():
    # A file cut mid-entry: continuation lines before the first timestamp
    # form a headless entry and are still classified (here: kept, because
    # one names the addon id).
    (_dest, text), _gui = _run_success(
        current=["  somewhere under script.audiooffsetmanager.evolved",
                 AOME_LINE])
    assert "somewhere under" in text


def test_both_logs_export_oldest_first_behind_labeled_dividers():
    old_line = AOME_LINE.replace("probed", "lastsession")
    (_dest, text), _gui = _run_success(old=[old_line], current=[AOME_LINE])
    assert "==== kodi.old.log ====" in text
    assert "==== kodi.log ====" in text
    assert text.index("==== kodi.old.log ====") \
        < text.index("lastsession") \
        < text.index("==== kodi.log ====") \
        < text.index("probed dolbyvision")


def test_missing_old_log_is_fine_and_leaves_no_divider():
    (_dest, text), _gui = _run_success(old=None, current=[AOME_LINE])
    assert "==== kodi.log ====" in text
    assert "kodi.old.log" not in text


def test_reader_failing_mid_stream_keeps_what_it_produced():
    def crashing():
        yield AOME_LINE
        raise IOError("rotated underneath the read")

    gui = FakeGui()
    gui.browse_answers = ['/reports/']
    writer = FakeWriter()
    view = LogExportView(
        gui,
        read_old_log=lambda: None,
        read_current_log=crashing,
        write_export=writer,
        clock=lambda: STAMP)
    view.run_export()
    _destination, text = _exported(writer)
    assert "AOMe_StreamDetector" in text


# -- rendering ----------------------------------------------------------------

def test_preamble_names_the_addon_and_version():
    (_dest, text), _gui = _run_success(current=[AOME_LINE],
                                       version='1.0.0~beta13')
    head = text.splitlines()[:4]
    assert head[0] == "Audio Offset Manager: Evolved filtered log export"
    assert head[1] == "Addon version: 1.0.0~beta13"
    assert head[2].startswith("Exported: ")
    assert "only this addon's log entries" in head[3]
    assert text.endswith("\n")


def test_redaction_folds_paths_longest_first_and_masks_credentials():
    line = ("2026-07-18 10:03:00.000 T:7 info <general>: AOMe_TransferView: "
            "exported 3 entries to smb://user:secret@nas/backups from "
            "C:\\Kodi\\home\\userdata\\profile\\ under C:\\Kodi\\home\\")
    (_dest, text), _gui = _run_success(
        current=[line],
        # Deliberately shortest-first: the view must reorder so the
        # profile (under home) folds before home swallows its prefix.
        redactions=[("C:\\Kodi\\home\\", "special://home/"),
                    ("C:\\Kodi\\home\\userdata\\profile\\",
                     "special://profile/")])
    assert "special://profile/ under special://home/" in text
    assert "C:\\Kodi" not in text
    assert "smb://USERNAME:PASSWORD@nas/backups" in text
    assert "secret" not in text


def test_user_home_pair_masks_a_picked_export_destination():
    # The field-caught leak (2026-07-18): the addon's own "exported ...
    # to <destination>" line names a user-picked folder (Desktop) that
    # sits under the OS profile but outside Kodi's home — the ~/ pair
    # the router now wires must mask it.
    line = ("2026-07-18 15:28:13.990 T:36684 info <general>: "
            "AOMe_LogExportView: exported filtered log to "
            "C:\\Users\\tester\\Desktop\\aome-log-20260718-152813.log")
    (_dest, text), _gui = _run_success(
        current=[line],
        redactions=[("C:\\Users\\tester\\", "~/")])
    assert "exported filtered log to ~/Desktop\\aome-log" in text
    assert "tester" not in text


def test_size_cap_drops_oldest_entries_and_says_so(monkeypatch):
    monkeypatch.setattr(logexport, '_MAX_EXPORT_BYTES', 200)
    first = AOME_LINE.replace("probed", "firstentry")
    last = AOME_LINE.replace("probed", "lastentry")
    (_dest, text), _gui = _run_success(current=[first, AOME_LINE, last])
    assert "lastentry" in text
    assert "firstentry" not in text
    assert "trimmed" in text


def test_uncapped_export_carries_no_trim_marker():
    (_dest, text), _gui = _run_success(current=[AOME_LINE])
    assert "trimmed" not in text
