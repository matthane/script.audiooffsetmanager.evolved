"""Script-process entry routing (the ``RunScript`` half of the addon).

Like ``runtime.py`` it sits at the ``aom`` package root, outside the
layered subpackages, and composes the Kodi pieces for its own process: the
service and the script run as SEPARATE processes whose only shared state is
the on-disk store — the script READS ``offsets.json`` through the read-only
reader and mutates it ONLY over the NotifyAll channel (single-writer
doctrine; D5 report-only when the service is absent).

Routes:

- ``manage_offsets`` — the stored-offsets management view (inspection +
  delete/clear, P6), reached from the settings dialog's action button.
- anything else / no argument — open the addon settings (D13: launching
  the addon opens the full settings dialog, the natural hub).

Every route ends in the settings dialog: the manage button closes it on
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
from resources.lib.aom.kodi.settings import ADDON_ID, STORE_PATH, Settings
from resources.lib.aom.store.offset_store import read_profiles
from resources.lib.aom.view.manage import ManageView


def handle_script_call(argv=None):
    """Route the RunScript argument (the script process's entry point).

    ``argv`` defaults to ``sys.argv``: RunScript(<id>,manage_offsets)
    arrives as ``argv[1]``.
    """
    args = sys.argv if argv is None else argv
    route = args[1] if len(args) > 1 else ''
    if route == 'manage_offsets':
        _manage_offsets()
        # ...then fall through: the manage button closed the settings
        # dialog, so every exit from the view lands back in it.
    xbmcaddon.Addon(ADDON_ID).openSettings()


def _manage_offsets():
    """Compose the view's process graph and run it.

    Mirrors the service runtime's composition style: one logger (with the
    same debug escalation the service uses), one gui, the read-only reader
    pointed at the shared STORE_PATH, and the mutation client as the ONLY
    write path.
    """
    logger = KodiLogger()
    settings = Settings(log=logger)
    logger.debug_escalation = settings.debug_logging_enabled()
    gui = Gui(log=logger)
    client = MutationClient(KodiGateway(log=logger), log=logger)
    store_path = xbmcvfs.translatePath(STORE_PATH)
    view = ManageView(
        lambda: read_profiles(store_path, log_debug=logger.debug),
        gui, client.send,
        per_fps=settings.per_fps_offsets_enabled(),
        log_debug=logger.debug)
    view.run()
