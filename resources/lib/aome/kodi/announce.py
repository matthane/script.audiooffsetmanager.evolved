"""Shared NotifyAll transport helpers for the store mutation channel.

Kodi surfaces a custom ``JSONRPC.NotifyAll`` message to monitors as
``Other.<message>`` with the payload re-serialized as a JSON string. Both
channel endpoints — the service's ``MonitorBridge`` and the script's
``MutationClient`` — must agree on that envelope exactly, so the
method-name composition and the payload decode live HERE, once — two
hand-rolled copies would always be one hardening fix away from drifting.

The PROTOCOL (message names, op vocabulary, ack fields) is app-layer and
lives in ``aome.app.store_mutations``; this module owns only the Kodi
transport dressing around it.
"""

import json

_OTHER_PREFIX = 'Other.'


def other_method(message):
    """The ``Monitor.onNotification`` method name for a NotifyAll message."""
    return _OTHER_PREFIX + message


def decode_payload(data):
    """Decode a notification's JSON payload dict; ``None`` when undecodable.

    Empty ``data`` decodes to ``{}`` (a request with no fields — the
    receiver's field validation rejects it loudly); invalid JSON or a
    non-dict document returns ``None`` so the caller chooses its own
    malformed-path behavior (the bridge posts a loud rejection, the client
    silently ignores — an ack we cannot read is indistinguishable from no
    ack).
    """
    try:
        payload = json.loads(data) if data else {}
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload
