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
dropping them back into Kodi.
"""

import sys

import xbmcaddon
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
        # every exit from a view lands back in it (same for all routes).
    elif route == 'export_offsets':
        _transfer_view().run_export()
    elif route == 'import_offsets':
        _transfer_view().run_import()
    xbmcaddon.Addon(ADDON_ID).openSettings()


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
