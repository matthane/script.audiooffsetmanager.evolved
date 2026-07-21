"""Unit tests for the script-process router.

Routing is pinned at the ``handle_script_call`` seam (the module-level
``_manage_offsets`` is monkeypatched so the routing test needs no Kodi
composition), and the view composition is pinned separately with a
recording stand-in for ``ManageView`` under Kodistubs.
"""

import os

import pytest
import xbmcaddon
import xbmcvfs

from resources.lib.aome import script_router
from resources.lib.aome.kodi.gui import Gui
from resources.lib.aome.kodi.mutation_client import MutationClient


class _StubSettings:
    def getBool(self, _setting_id):
        return False

    def getInt(self, _setting_id):
        return 0


class RecordingAddon:
    """xbmcaddon.Addon stand-in: records openSettings, and carries just
    enough surface (getSettings/getAddonInfo/getLocalizedString) for the
    router's Settings/Gui composition to construct."""

    instances = []

    def __init__(self, addon_id=None):
        self.addon_id = addon_id
        self.opened = 0
        RecordingAddon.instances.append(self)

    def openSettings(self):
        self.opened += 1

    def getSettings(self):
        return _StubSettings()

    def getAddonInfo(self, _prop):
        return ""

    def getLocalizedString(self, _string_id):
        return ""


@pytest.fixture(autouse=True)
def recording_addon(monkeypatch):
    RecordingAddon.instances = []
    monkeypatch.setattr(xbmcaddon, 'Addon', RecordingAddon)
    return RecordingAddon


def test_default_route_opens_the_settings_dialog():
    script_router.handle_script_call(['script.py'])

    assert len(RecordingAddon.instances) == 1
    assert RecordingAddon.instances[0].addon_id == \
        'script.audiooffsetmanager.evolved'
    assert RecordingAddon.instances[0].opened == 1


def test_unknown_route_falls_back_to_settings():
    script_router.handle_script_call(['script.py', 'play_test_video'])
    assert RecordingAddon.instances[0].opened == 1


def test_manage_offsets_runs_the_view_then_returns_to_settings(monkeypatch):
    # Record how many openSettings() calls had happened when the view ran:
    # the settings dialog must reopen AFTER the view exits (the manage
    # button closed it via <close>true</close>), never before — the view
    # must not render on top of a pending save-on-close.
    calls = []
    monkeypatch.setattr(
        script_router, '_manage_offsets',
        lambda: calls.append(sum(a.opened for a in RecordingAddon.instances)))

    script_router.handle_script_call(['script.py', 'manage_offsets'])

    assert calls == [0]                    # view ran, settings still closed
    assert sum(a.opened for a in RecordingAddon.instances) == 1


def test_manage_offsets_composition(monkeypatch, tmp_path):
    # The view graph: read-only reader on the shared store path, the real
    # Gui surface, and the mutation client's send as the ONLY write path.
    built = {}

    class FakeView:
        def __init__(self, read_entries, gui, send_mutation, *,
                     per_fps=False, distinct_spatial=True, current_key=None,
                     log_debug=None):
            built['reader'] = read_entries
            built['gui'] = gui
            built['send'] = send_mutation
            built['per_fps'] = per_fps
            built['distinct_spatial'] = distinct_spatial
            built['current_key'] = current_key
            built['log'] = log_debug
            built['ran'] = 0

        def run(self):
            built['ran'] += 1

    monkeypatch.setattr(script_router, 'ManageView', FakeView)
    monkeypatch.setattr(xbmcvfs, 'translatePath',
                        lambda _p: str(tmp_path / 'offsets.json'))

    script_router._manage_offsets()

    assert built['ran'] == 1
    assert built['reader']() == {}                 # read-only reader, no file
    assert isinstance(built['gui'], Gui)
    assert built['per_fps'] is False               # the live toggle read
    method = built['send']
    assert getattr(method, '__name__', '') == 'send'
    assert isinstance(getattr(method, '__self__', None), MutationClient)
    # The playing-profile seam reads the service's published property
    # (unset here -> '' -> "nothing playing").
    assert built['current_key']() == ''
    assert callable(built['log'])


class RecordingTransfer:
    """Stand-in for the composed TransferView: records which flow ran."""

    ran = []

    def run_export(self):
        RecordingTransfer.ran.append('export')

    def run_import(self):
        RecordingTransfer.ran.append('import')


class _IdleMonitor:
    """xbmc.Monitor stand-in: never aborting, never sleeping."""

    def waitForAbort(self, _timeout=0):
        return False


def _wire_focus_reopen(monkeypatch, dialog_ids):
    """Fake the Kodi surface of _reopen_settings_at_advanced.

    ``dialog_ids`` is the sequence getCurrentWindowDialogId answers with
    (the last value repeats), simulating the dialog appearing — or not.
    Returns the recorded executebuiltin list.
    """
    import xbmc
    import xbmcgui
    builtins = []
    remaining = list(dialog_ids)
    monkeypatch.setattr(xbmc, 'executebuiltin', builtins.append)
    monkeypatch.setattr(xbmc, 'sleep', lambda _ms: None)
    monkeypatch.setattr(xbmc, 'Monitor', _IdleMonitor)
    monkeypatch.setattr(
        xbmcgui, 'getCurrentWindowDialogId',
        lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0])
    return builtins


class RecordingLogExport:
    """Stand-in for the composed LogExportView: records the run."""

    ran = 0

    def run_export(self):
        RecordingLogExport.ran += 1


def test_export_log_route_runs_the_view_then_reopens_at_advanced(
        monkeypatch):
    # The log-export button lives in Advanced too: same close-then-reopen
    # arc as the transfer routes, never the plain openSettings().
    RecordingLogExport.ran = 0
    monkeypatch.setattr(script_router, '_log_export_view',
                        RecordingLogExport)
    builtins = _wire_focus_reopen(
        monkeypatch, [0, script_router.SETTINGS_DIALOG_ID])

    script_router.handle_script_call(['script.py', 'export_log'])

    assert RecordingLogExport.ran == 1
    assert builtins == [
        'Addon.OpenSettings(script.audiooffsetmanager.evolved)',
        'SetFocus({0})'.format(script_router.ADVANCED_CATEGORY_FOCUS),
    ]
    assert sum(a.opened for a in RecordingAddon.instances) == 0


@pytest.mark.parametrize("route,flow", [
    ('export_offsets', 'export'),
    ('import_offsets', 'import'),
])
def test_transfer_routes_reopen_settings_focused_on_advanced(
        monkeypatch, route, flow):
    # The transfer buttons live in Advanced: the reopen must land there —
    # builtin open, WAIT for the dialog to actually be active (a focus
    # issued into the previous window is silently dropped), then the
    # category focus. Never the plain openSettings() call that always
    # lands on the first category.
    RecordingTransfer.ran = []
    monkeypatch.setattr(script_router, '_transfer_view', RecordingTransfer)
    # The dialog appears after a couple of polls, as on a real box.
    builtins = _wire_focus_reopen(
        monkeypatch, [0, 0, script_router.SETTINGS_DIALOG_ID])

    script_router.handle_script_call(['script.py', route])

    assert RecordingTransfer.ran == [flow]
    assert builtins == [
        'Addon.OpenSettings(script.audiooffsetmanager.evolved)',
        'SetFocus({0})'.format(script_router.ADVANCED_CATEGORY_FOCUS),
    ]
    assert sum(a.opened for a in RecordingAddon.instances) == 0


def test_focus_reopen_gives_up_when_the_dialog_never_appears(monkeypatch):
    # A dialog that never becomes active (whatever swallowed it) must
    # degrade to the default landing — no SetFocus fired into some other
    # window, and the wait is bounded so the script process still exits.
    RecordingTransfer.ran = []
    monkeypatch.setattr(script_router, '_transfer_view', RecordingTransfer)
    builtins = _wire_focus_reopen(monkeypatch, [0])

    script_router.handle_script_call(['script.py', 'export_offsets'])

    assert builtins == [
        'Addon.OpenSettings(script.audiooffsetmanager.evolved)',
    ]


def test_advanced_focus_id_matches_the_settings_xml_category_order():
    # The dialog's category buttons get CONTROL_SETTINGS_START_BUTTONS
    # (-200, verified in xbmc source for Kodi 21 AND 22) + index; the
    # constant is only right while 'advanced' keeps its position in
    # settings.xml — a reorder must fail HERE, not in the field as a
    # focus landing on the wrong category.
    import xml.etree.ElementTree as ET
    from pathlib import Path

    settings_xml = (Path(script_router.__file__).resolve().parents[2]
                    / 'settings.xml')
    categories = [element.get('id')
                  for element in ET.parse(str(settings_xml)).iter('category')]
    assert script_router.ADVANCED_CATEGORY_FOCUS == \
        script_router.CONTROL_SETTINGS_START_BUTTONS \
        + categories.index('advanced')


def test_transfer_view_composition(monkeypatch, tmp_path):
    # The backup surface's graph: readers on the shared store/staging
    # paths (staging = store + IMPORT_SUFFIX, the channel protocol
    # constant), the real Gui, and the mutation client's send as the only
    # channel leg.
    from resources.lib.aome.store.offset_store import StoreUnreadable

    built = {}

    class FakeTransfer:
        def __init__(self, gui, send_mutation, **kwargs):
            built['gui'] = gui
            built['send'] = send_mutation
            built.update(kwargs)

    monkeypatch.setattr(script_router, 'TransferView', FakeTransfer)
    monkeypatch.setattr(xbmcvfs, 'translatePath',
                        lambda _p: str(tmp_path / 'offsets.json'))

    copies = []
    monkeypatch.setattr(xbmcvfs, 'copy',
                        lambda src, dst: copies.append((src, dst)) or True)
    deletes = []
    monkeypatch.setattr(xbmcvfs, 'delete', lambda p: deletes.append(p))

    script_router._transfer_view()

    assert isinstance(built['gui'], Gui)
    method = built['send']
    assert getattr(method, '__name__', '') == 'send'
    assert isinstance(getattr(method, '__self__', None), MutationClient)

    store_path = str(tmp_path / 'offsets.json')
    staging_path = store_path + '.import'
    assert built['read_entries']() == {}          # read-only reader, no file
    with pytest.raises(StoreUnreadable):
        built['read_staged']()                    # missing staging = error

    assert built['export_file']('/backups/x.json') is True
    assert copies[-1] == (store_path, '/backups/x.json')
    assert built['stage_file']('/downloads/y.json') is True
    assert copies[-1] == ('/downloads/y.json', staging_path)
    built['discard_staged']()
    assert deletes == [staging_path]


def test_log_export_view_composition(monkeypatch, tmp_path):
    # The support-report surface's graph: line streams over the two log
    # files (None when absent, streaming when present), the xbmcvfs
    # writer, the redaction pairs in both separator spellings, and the
    # addon version for the export preamble. No mutation client leg: the
    # flow is read-only everywhere.
    built = {}

    class FakeLogExport:
        def __init__(self, gui, **kwargs):
            built['gui'] = gui
            built.update(kwargs)

    class RecordingVfsFile:
        instances = []

        def __init__(self, path, mode=None):
            self.path = path
            self.mode = mode
            self.written = None
            self.closed = False
            RecordingVfsFile.instances.append(self)

        def write(self, text):
            self.written = text
            return True

        def close(self):
            self.closed = True

    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    (log_dir / 'kodi.log').write_text(
        "2026-07-18 10:00:00.000 T:1 info <general>: AOMe_Runtime: line\n",
        encoding='utf-8')
    home = str(tmp_path / 'kodi_home') + os.sep

    def fake_translate(path):
        if path == 'special://logpath/':
            return str(log_dir) + os.sep
        if path == 'special://home/':
            return home
        return path                       # special://profile/ unresolved

    monkeypatch.setattr(script_router, 'LogExportView', FakeLogExport)
    monkeypatch.setattr(xbmcvfs, 'translatePath', fake_translate)
    monkeypatch.setattr(xbmcvfs, 'File', RecordingVfsFile)
    monkeypatch.setattr(script_router.os.path, 'expanduser',
                        lambda _p: 'C:\\Users\\tester')
    RecordingVfsFile.instances = []

    script_router._log_export_view()

    assert isinstance(built['gui'], Gui)
    assert 'send_mutation' not in built

    # kodi.old.log is absent -> None; kodi.log streams its lines.
    assert built['read_old_log']() is None
    assert [line.rstrip('\n') for line in built['read_current_log']()] == \
        ["2026-07-18 10:00:00.000 T:1 info <general>: AOMe_Runtime: line"]

    # An unresolvable special:// root contributes no pair; the resolved
    # Kodi home arrives in both separator spellings, and the OS user
    # profile folds to ~/ (the field-caught leak: a user-picked export
    # destination under the OS profile sits outside Kodi's home). The
    # home prefix is native to the host platform, so its alternate
    # spelling swaps whichever separator it actually carries.
    alt_home = (home.replace('\\', '/') if '\\' in home
                else home.replace('/', '\\'))
    assert built['redactions'] == [
        (home, 'special://home/'),
        (alt_home, 'special://home/'),
        ('C:\\Users\\tester' + os.sep, '~/'),
        ('C:/Users/tester/', '~/'),
    ]

    assert built['write_export']('/reports/aome-log.log', 'text') is True
    handle = RecordingVfsFile.instances[-1]
    assert (handle.path, handle.mode) == ('/reports/aome-log.log', 'w')
    assert handle.written == 'text'
    assert handle.closed is True

    assert built['version'] == ""         # RecordingAddon's stub info
    assert callable(built['log_debug'])
