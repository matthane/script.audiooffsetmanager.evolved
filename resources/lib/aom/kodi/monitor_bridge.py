"""Kodi monitor bridge: settings-change + NotifyAll callbacks post to the dispatcher.

Also serves as the service's abort monitor (the runtime blocks on
``waitForAbort()`` of this instance). Near-zero logic, like the player
bridge: ``onNotification`` only FILTERS (own addon id, mutation message)
and decodes the JSON payload — the fields travel verbatim on the typed
event and the StoreMutationHandler owns all validation, so a malformed
payload is rejected loudly there instead of vanishing here.
"""

import json

import xbmc

from resources.lib.aom.app import events
from resources.lib.aom.app.store_mutations import MUTATION_MESSAGE
from resources.lib.aom.kodi.settings import ADDON_ID

# Kodi surfaces custom NotifyAll messages to monitors as 'Other.<message>'.
_MUTATION_METHOD = 'Other.' + MUTATION_MESSAGE


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
        try:
            payload = json.loads(data) if data else {}
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            # Undecodable request: post it anyway (op=None) so the handler
            # rejects it LOUDLY instead of the channel silently eating it.
            self._dispatcher.post(events.StoreMutationRequested(op=None))
            return
        self._dispatcher.post(events.StoreMutationRequested(
            op=payload.get('op'),
            key=payload.get('key'),
            request_id=payload.get('request_id')))
