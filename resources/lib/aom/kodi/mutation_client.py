"""Script-process client for the store mutation channel (D5).

The management view's ONLY write path: one ``JSONRPC.NotifyAll`` request to
the service, then a bounded poll for the matching ack. The service's
dispatcher executes the mutation (single-writer doctrine — see
``aom.app.store_mutations``) and acks back over NotifyAll; ``send`` returns
that ack dict, or ``None`` when no ack arrives inside the timeout — the
view's "service not running" signal. There is deliberately NO fallback
write path here (D5: report-only).

Request/ack matching uses a per-request ``request_id`` echoed by the
service, so a stale ack from an earlier attempt can never satisfy a newer
request. ``onNotification`` runs on Kodi's announce thread while ``send``
polls; the single-reference handoff (assign whole dict, read whole dict)
is safe under the GIL.

This is an ``aom.kodi`` adapter: the only aom layer permitted to import
``xbmc``.
"""

import json
import uuid

import xbmc

from resources.lib.aom.app.store_mutations import (ACK_MESSAGE,
                                                   MUTATION_MESSAGE)
from resources.lib.aom.kodi.settings import ADDON_ID

# Kodi surfaces custom NotifyAll messages to monitors as 'Other.<message>'.
_ACK_METHOD = 'Other.' + ACK_MESSAGE


class MutationClient(xbmc.Monitor):
    """Sends one mutation request at a time and waits for the service's ack."""

    TIMEOUT_SECONDS = 3.0
    POLL_SECONDS = 0.1

    def __init__(self, *, log):
        """``log`` is a REQUIRED ``(message, level)`` sink (house convention)."""
        super().__init__()
        self._log = log
        self._pending_id = None
        self._reply = None

    # -- Kodi announce thread ----------------------------------------------------

    def onNotification(self, sender, method, data):
        if sender != ADDON_ID or method != _ACK_METHOD:
            return
        try:
            payload = json.loads(data) if data else {}
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        if payload.get('request_id') != self._pending_id:
            return
        self._reply = payload

    # -- script thread -------------------------------------------------------------

    def send(self, op, key=None):
        """Broadcast one mutation request; return the ack dict or None.

        ``None`` means the broadcast failed or no ack arrived within
        ``TIMEOUT_SECONDS`` — the caller treats both as "service not
        running" (D5 report-only). The poll uses ``waitForAbort`` slices so
        a Kodi shutdown mid-wait aborts cleanly.
        """
        request_id = uuid.uuid4().hex
        self._pending_id = request_id
        self._reply = None

        payload = {'op': op, 'request_id': request_id}
        if key is not None:
            payload['key'] = key

        try:
            response = json.loads(xbmc.executeJSONRPC(json.dumps({
                'jsonrpc': '2.0',
                'method': 'JSONRPC.NotifyAll',
                'params': {
                    'sender': ADDON_ID,
                    'message': MUTATION_MESSAGE,
                    'data': payload,
                },
                'id': 1,
            })))
        except Exception as e:
            self._log(f"AOM_MutationClient: Error broadcasting {op}: "
                      f"{str(e)}", xbmc.LOGERROR)
            return None
        if 'error' in response:
            self._log(f"AOM_MutationClient: Failed to broadcast {op}: "
                      f"{response['error']}", xbmc.LOGWARNING)
            return None

        waited = 0.0
        while waited < self.TIMEOUT_SECONDS:
            if self._reply is not None:
                return self._reply
            if self.waitForAbort(self.POLL_SECONDS):
                return None
            waited += self.POLL_SECONDS
        self._log(f"AOM_MutationClient: No ack for {op} within "
                  f"{self.TIMEOUT_SECONDS}s (service not running?)",
                  xbmc.LOGDEBUG)
        return None
