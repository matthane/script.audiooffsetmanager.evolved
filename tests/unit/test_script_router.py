"""Unit tests for the script-process router.

Routing is pinned at the ``handle_script_call`` seam (the module-level
``_manage_offsets`` is monkeypatched so the routing test needs no Kodi
composition), and the view composition is pinned separately with a
recording stand-in for ``ManageView`` under Kodistubs.
"""

import pytest
import xbmcaddon
import xbmcvfs

from resources.lib.aom import script_router
from resources.lib.aom.kodi.gui import Gui
from resources.lib.aom.kodi.mutation_client import MutationClient


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
        'script.audiooffsetmanagerevolved'
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
                     log_debug=None):
            built['reader'] = read_entries
            built['gui'] = gui
            built['send'] = send_mutation
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
    method = built['send']
    assert getattr(method, '__name__', '') == 'send'
    assert isinstance(getattr(method, '__self__', None), MutationClient)
    assert callable(built['log'])
