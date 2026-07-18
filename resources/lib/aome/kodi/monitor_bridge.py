"""Kodi monitor bridge: settings-change + NotifyAll callbacks post to the dispatcher.

Also serves as the service's abort monitor (the runtime blocks on
``waitForAbort()`` of this instance). Near-zero logic, like the player
bridge: ``onNotification`` only FILTERS (own addon id, mutation message)
and decodes the JSON payload via the shared transport helpers — the fields
travel verbatim on the typed event and the StoreMutationHandler owns all
validation, so a malformed payload is rejected loudly there instead of
vanishing here.
"""

import xbmc

from resources.lib.aome.app import events
from resources.lib.aome.app.store_mutations import MUTATION_MESSAGE
from resources.lib.aome.kodi.announce import decode_payload, other_method
from resources.lib.aome.kodi.settings import ADDON_ID

_MUTATION_METHOD = other_method(MUTATION_MESSAGE)


class MonitorBridge(xbmc.Monitor):
    def __init__(self, dispatcher):
        super().__init__()
        self._dispatcher = dispatcher

    def onSettingsChanged(self):
        self._dispatcher.post(events.SettingsChanged())

    def onNotification(self, sender, method, data):
        # Only the addon's own mutation channel; everything else on the bus
        # (including the service's own acks, which use a different message
        # name) is not ours to handle.
        if sender != ADDON_ID or method != _MUTATION_METHOD:
            return
        payload = decode_payload(data)
        if payload is None:
            # Undecodable request: post it anyway (op=None) so the handler
            # rejects it LOUDLY instead of the channel silently eating it.
            self._dispatcher.post(events.StoreMutationRequested(op=None))
            return
        self._dispatcher.post(events.StoreMutationRequested(
            op=payload.get('op'),
            key=payload.get('key'),
            request_id=payload.get('request_id')))
