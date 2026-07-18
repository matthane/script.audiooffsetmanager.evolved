"""Unit tests for the ``Gui.select`` adapter's two-line detail rows.

The manage view sends plain strings and/or ``(label, detail)`` tuples; the
adapter must convert a mixed list to ListItems and upgrade the dialog to
``useDetails``, while an all-strings list stays on the classic plain call.
Kodistubs' ``xbmcgui`` classes are monkeypatched with recorders because the
stubs do not retain constructor arguments.
"""

import xbmcgui

from resources.lib.aom.kodi.gui import Gui


class FakeListItem:
    def __init__(self, label='', label2='', path='', offscreen=False):
        self.label = label
        self.label2 = label2
        self.offscreen = offscreen


class FakeDialog:
    """Records the last select() call on the CLASS (a fresh instance is
    constructed per call inside the adapter)."""

    last = None

    def select(self, heading, options, useDetails=False):
        FakeDialog.last = (heading, options, useDetails)
        return 1


def _gui(monkeypatch):
    monkeypatch.setattr(xbmcgui, 'ListItem', FakeListItem)
    monkeypatch.setattr(xbmcgui, 'Dialog', FakeDialog)
    FakeDialog.last = None
    return Gui(log=lambda message, level=0: None)


def test_tuple_options_become_detail_listitems(monkeypatch):
    gui = _gui(monkeypatch)

    result = gui.select("heading", [
        ("Dolby Vision | All rates | TrueHD", "-115 ms (user, 2026-07-15)"),
        "Clear all stored offsets",
    ])

    assert result == 1
    heading, items, use_details = FakeDialog.last
    assert heading == "heading"
    assert use_details is True
    assert [(i.label, i.label2) for i in items] == [
        ("Dolby Vision | All rates | TrueHD", "-115 ms (user, 2026-07-15)"),
        ("Clear all stored offsets", ""),
    ]
    assert all(i.offscreen for i in items)


def test_all_string_options_stay_on_the_plain_call(monkeypatch):
    gui = _gui(monkeypatch)

    gui.select("heading", ["a", "b"])

    heading, items, use_details = FakeDialog.last
    assert use_details is False
    assert items == ["a", "b"]          # untouched strings, no ListItems


def test_select_failure_reads_as_cancel(monkeypatch):
    gui = _gui(monkeypatch)

    def boom(*_args, **_kwargs):
        raise RuntimeError("gui layer down")

    monkeypatch.setattr(FakeDialog, 'select', boom)
    assert gui.select("heading", [("a", "b")]) == -1


# -- browse adapters (the transfer view's pickers) ----------------------------

class BrowsingDialog:
    last = None

    def browseSingle(self, type_, heading, shares, mask=''):
        BrowsingDialog.last = (type_, heading, shares, mask)
        return '/picked'


def _browsing_gui(monkeypatch):
    monkeypatch.setattr(xbmcgui, 'Dialog', BrowsingDialog)
    BrowsingDialog.last = None
    return Gui(log=lambda message, level=0: None)


def test_browse_folder_is_the_writeable_directory_picker(monkeypatch):
    gui = _browsing_gui(monkeypatch)
    assert gui.browse_folder("heading") == '/picked'
    # Type 3 = ShowAndGetWriteableDirectory over the files shares.
    assert BrowsingDialog.last == (3, "heading", 'files', '')


def test_browse_file_passes_the_extension_mask(monkeypatch):
    gui = _browsing_gui(monkeypatch)
    assert gui.browse_file("heading", '.json') == '/picked'
    # Type 1 = ShowAndGetFile.
    assert BrowsingDialog.last == (1, "heading", 'files', '.json')


def test_browse_failure_reads_as_cancel(monkeypatch):
    gui = _browsing_gui(monkeypatch)

    def boom(*_args, **_kwargs):
        raise RuntimeError("gui layer down")

    monkeypatch.setattr(BrowsingDialog, 'browseSingle', boom)
    assert gui.browse_folder("heading") == ''
    assert gui.browse_file("heading", '.json') == ''
