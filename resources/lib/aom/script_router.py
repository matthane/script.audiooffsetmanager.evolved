"""Script-process entry routing (the ``RunScript`` half of the addon).

Replaces ``aom/onboarding.py`` — the onboarding apparatus (test video,
bypass button, ``new_install`` choreography) is DELETED with its feature
(P1: zero-config install; an empty store simply does nothing until taught),
not ported. What remains is a thin router. Like ``runtime.py`` it sits at
the ``aom`` package root, outside the layered subpackages, and wires the
Kodi pieces for its own process: the service and the script run as SEPARATE
processes whose only shared state is the on-disk stores.

Routes:

- ``manage_offsets`` — the stored-offsets management view (lands in Phase
  E4; until then it opens the settings dialog so the settings.xml action
  button is never a dead end).
- anything else / no argument — open the addon settings (D13: launching
  the addon opens the full settings dialog, the natural hub).
"""

import xbmcaddon

from resources.lib.aom.kodi.settings import ADDON_ID


def handle_script_call():
    """Route the RunScript argument (the script process's entry point).

    Every route currently lands on the settings dialog. Phase E4 splits
    ``manage_offsets`` (read via ``sys.argv[1]``) out to the management
    view; until the targets actually differ there is deliberately no
    branch to mis-wire.
    """
    xbmcaddon.Addon(ADDON_ID).openSettings()
