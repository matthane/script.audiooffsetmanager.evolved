"""Script-process entry routing (the ``RunScript`` half of the addon).

Like ``runtime.py`` it sits at the ``aom`` package root, outside the
layered subpackages, and composes the Kodi pieces for its own process: the
service and the script run as SEPARATE processes whose only shared state is
the on-disk store — the script READS ``offsets.json`` through the read-only
reader and mutates it ONLY over the NotifyAll channel (single-writer
doctrine; D5 report-only when the service is absent). The import route
additionally writes the ``.import`` STAGING file — a sibling the service
consumes, never the store file itself.

Routes:

- ``manage_offsets`` — the stored-offsets management view (inspection +
  delete/clear, P6), reached from the settings dialog's action button.
- ``export_offsets`` / ``import_offsets`` — the backup surface (the
  TransferView): verbatim file export to a picked folder, and the staged
  restore over the mutation channel's ``import`` op.
- anything else / no argument — open the addon settings (D13: launching
  the addon opens the full settings dialog, the natural hub).

Every route ends in the settings dialog: the action buttons close it on
press (``<close>true</close>``, the write-ordering doctrine), so reopening
after the view exits returns the user to where they came from instead of
dropping them back into Kodi. The transfer routes reopen FOCUSED on the
Advanced category (their buttons' home) — a plain ``openSettings()``
always lands on the first category, which field-read as being teleported
away from where you were.
"""

import sys

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from resources.lib.aom.kodi.gateway import KodiGateway
from resources.lib.aom.kodi.gui import Gui
from resources.lib.aom.kodi.log import KodiLogger
from resources.lib.aom.kodi.mutation_client import MutationClient
from resources.lib.aom.kodi.settings import (ADDON_ID, STORE_PATH, Settings,
                                             import_staging_path)
from resources.lib.aom.store.offset_store import read_import, read_profiles
from resources.lib.aom.view.manage import ManageView
from resources.lib.aom.view.transfer import TransferView


def handle_script_call(argv=None):
    """Route the RunScript argument (the script process's entry point).

    ``argv`` defaults to ``sys.argv``: RunScript(<id>,manage_offsets)
    arrives as ``argv[1]``.
    """
    args = sys.argv if argv is None else argv
    route = args[1] if len(args) > 1 else ''
    if route == 'manage_offsets':
        _manage_offsets()
        # ...then fall through: the button closed the settings dialog, so
        # every exit from a view lands back in it. The manage button
        # lives in the FIRST category, which is where a plain reopen
        # lands anyway; the transfer routes reopen focused on Advanced.
    elif route == 'export_offsets':
        _transfer_view().run_export()
        _reopen_settings_at_advanced()
        return
    elif route == 'import_offsets':
        _transfer_view().run_import()
        _reopen_settings_at_advanced()
        return
    xbmcaddon.Addon(ADDON_ID).openSettings()


# The settings dialog assigns its category buttons the control ids
# CONTROL_SETTINGS_START_BUTTONS + category index — created by
# CGUIDialogSettingsBase itself, so skin-independent. The base is -200
# (verified in xbmc source for BOTH field versions: Kodi 21 Omega and
# Kodi 22 master; focusing the button is what switches the displayed
# category — the GUI_MSG_FOCUSED handler). Advanced is the THIRD
# category in settings.xml (index 2) -> -198; the router test derives
# this constant from the XML so a category reorder fails loudly.
CONTROL_SETTINGS_START_BUTTONS = -200
ADVANCED_CATEGORY_FOCUS = CONTROL_SETTINGS_START_BUTTONS + 2

# WINDOW_DIALOG_ADDON_SETTINGS: the wait-for-dialog target below.
SETTINGS_DIALOG_ID = 10140

_FOCUS_WAIT_SECONDS = 2.0    # give the dialog this long to appear
_FOCUS_POLL_SECONDS = 0.05
_FOCUS_SETTLE_MS = 100       # one beat for the dialog's controls to build


def _reopen_settings_at_advanced():
    """Reopen the settings dialog landed on Advanced — the category the
    export/import buttons live in, where the user last was.

    The builtin form is used because ``openSettings()`` always lands on
    the first category. ``Addon.OpenSettings`` only QUEUES the dialog
    open: a ``SetFocus`` issued back-to-back fires while the previous
    window is still active and is silently dropped (field-observed on
    Kodi 22), so this waits until the addon-settings dialog is actually
    the active dialog, lets its controls build for a beat, and only then
    focuses the Advanced category button. Every bail-out path (dialog
    never appears, Kodi shutting down) degrades to the dialog's default
    first-category landing — the plain-reopen behavior, never an error.
    """
    xbmc.executebuiltin('Addon.OpenSettings({0})'.format(ADDON_ID))
    monitor = xbmc.Monitor()
    waited = 0.0
    while xbmcgui.getCurrentWindowDialogId() != SETTINGS_DIALOG_ID:
        if waited >= _FOCUS_WAIT_SECONDS:
            return
        if monitor.waitForAbort(_FOCUS_POLL_SECONDS):
            return
        waited += _FOCUS_POLL_SECONDS
    xbmc.sleep(_FOCUS_SETTLE_MS)
    xbmc.executebuiltin('SetFocus({0})'.format(ADVANCED_CATEGORY_FOCUS))


def _script_graph():
    """The per-route composition preamble, written once for every route:
    one logger (with the same debug escalation the service uses), the
    live settings proxy, the plain-dialog gui, and the mutation client as
    the ONLY write path to the store."""
    logger = KodiLogger()
    settings = Settings(log=logger)
    logger.debug_escalation = settings.debug_logging_enabled()
    gui = Gui(log=logger)
    client = MutationClient(KodiGateway(log=logger), log=logger)
    return logger, settings, gui, client


def _manage_offsets():
    """Compose the management view's process graph and run it: the shared
    preamble plus the read-only reader pointed at the shared STORE_PATH."""
    logger, settings, gui, client = _script_graph()
    store_path = xbmcvfs.translatePath(STORE_PATH)
    view = ManageView(
        lambda: read_profiles(store_path, log_debug=logger.debug),
        gui, client.send,
        per_fps=settings.per_fps_offsets_enabled(),
        log_debug=logger.debug)
    view.run()


def _transfer_view():
    """Compose the backup surface's process graph (export/import routes).

    The shared preamble plus the file seams: the read-only readers on the
    shared store/staging paths, and ``xbmcvfs`` as the copy/delete engine
    so VFS sources and destinations (smb://, nfs://, USB mounts) all
    work. The staging path comes from ``import_staging_path()`` — the one
    derivation both processes share.
    """
    logger, _settings, gui, client = _script_graph()
    store_path = xbmcvfs.translatePath(STORE_PATH)
    staging_path = import_staging_path()
    return TransferView(
        gui, client.send,
        read_entries=lambda: read_profiles(store_path,
                                           log_debug=logger.debug),
        read_staged=lambda: read_import(staging_path,
                                        log_debug=logger.debug),
        export_file=lambda destination: bool(
            xbmcvfs.copy(store_path, destination)),
        stage_file=lambda source: bool(xbmcvfs.copy(source, staging_path)),
        discard_staged=lambda: xbmcvfs.delete(staging_path),
        log_debug=logger.debug)
