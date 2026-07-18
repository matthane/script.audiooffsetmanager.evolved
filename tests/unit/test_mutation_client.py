"""Unit tests for the script-process mutation channel client.

The broadcast leg goes through an injected recording gateway (the E4-review
consolidation: the RPC envelope lives only in ``KodiGateway.notify_all``);
acks are synthesized by calling ``onNotification`` directly — the same
delivery path Kodi's announce thread uses. ``waitForAbort`` is patched to
advance without real sleeping so the timeout path runs instantly.
"""

import json

import pytest

from resources.lib.aome.app.store_mutations import (ACK_MESSAGE,
                                                   MUTATION_MESSAGE)
from resources.lib.aome.kodi.mutation_client import MutationClient
from resources.lib.aome.kodi.settings import ADDON_ID


ACK_METHOD = 'Other.' + ACK_MESSAGE


class RecordingGateway:
    """notify_all recorder; optionally answers each broadcast via a hook."""

    def __init__(self, result=True, on_send=None):
        self.sent = []                # (sender, message, data)
        self.result = result
        self.on_send = on_send

    def notify_all(self, sender, message, data):
        self.sent.append((sender, message, data))
        if self.on_send is not None:
            self.on_send(data)
        return self.result


def make_client(monkeypatch, gateway):
    logs = []
    client = MutationClient(gateway,
                            log=lambda message, level=None: logs.append(message))
    client.test_logs = logs
    # Never really block: abort is never requested in these tests.
    monkeypatch.setattr(MutationClient, 'waitForAbort',
                        lambda self, timeout=0: False)
    return client


def test_send_broadcasts_request_and_returns_matching_ack(monkeypatch):
    gateway = RecordingGateway()
    client = make_client(monkeypatch, gateway)
    # The service answers instantly: echo the request_id on the ack.
    gateway.on_send = lambda data: client.onNotification(
        ADDON_ID, ACK_METHOD, json.dumps(
            {'ok': True, 'detail': 'deleted',
             'request_id': data['request_id']}))

    reply = client.send('delete', key='dolbyvision|all|truehd')

    assert reply['ok'] is True and reply['detail'] == 'deleted'
    sender, message, data = gateway.sent[0]
    assert sender == ADDON_ID
    assert message == MUTATION_MESSAGE
    assert data['op'] == 'delete'
    assert data['key'] == 'dolbyvision|all|truehd'
    assert data['request_id']                      # non-empty match token


def test_clear_request_omits_the_key_field(monkeypatch):
    gateway = RecordingGateway()
    client = make_client(monkeypatch, gateway)

    client.send('clear')

    assert 'key' not in gateway.sent[0][2]


def test_timeout_without_ack_returns_none(monkeypatch):
    client = make_client(monkeypatch, RecordingGateway())

    assert client.send('delete', key='k|all|a') is None
    assert any('No ack' in line for line in client.test_logs)


def test_stale_ack_from_an_earlier_request_is_ignored(monkeypatch):
    client = make_client(monkeypatch, RecordingGateway())

    # An ack for some OTHER request id arrives while waiting: must not
    # satisfy this send.
    monkeypatch.setattr(
        MutationClient, 'waitForAbort',
        lambda self, timeout=0: self.onNotification(
            ADDON_ID, ACK_METHOD, json.dumps(
                {'ok': True, 'request_id': 'stale-previous-request'}))
        or False)

    assert client.send('clear') is None


def test_ack_while_idle_is_ignored_even_with_null_request_id(monkeypatch):
    # E4 review: the idle _pending_id is None, and a malformed/foreign ack
    # can carry request_id None — the idle guard must reject it rather
    # than letting None == None pre-seed a reply.
    client = make_client(monkeypatch, RecordingGateway())

    client.onNotification(ADDON_ID, ACK_METHOD,
                          '{"ok": false, "request_id": null}')
    client.onNotification(ADDON_ID, ACK_METHOD, '{"ok": false}')

    assert client._reply is None


def test_foreign_and_malformed_acks_are_ignored(monkeypatch):
    client = make_client(monkeypatch, RecordingGateway())
    client._pending_id = 'live-request'

    client.onNotification('some.other.addon', ACK_METHOD,
                          '{"request_id": "live-request", "ok": true}')
    client.onNotification(ADDON_ID, 'Other.' + MUTATION_MESSAGE,
                          '{"request_id": "live-request", "ok": true}')
    client.onNotification(ADDON_ID, ACK_METHOD, 'not json {{{')
    client.onNotification(ADDON_ID, ACK_METHOD, '[1, 2]')

    assert client._reply is None


def test_broadcast_failure_returns_none_without_waiting(monkeypatch):
    waits = []
    gateway = RecordingGateway(result=False)
    client = MutationClient(gateway, log=lambda m, level=None: None)
    monkeypatch.setattr(MutationClient, 'waitForAbort',
                        lambda self, timeout=0: waits.append(timeout) or False)

    assert client.send('delete', key='k|all|a') is None
    assert waits == []                              # no poll after a failed leg
    assert client._pending_id is None               # idle again


def test_abort_during_wait_returns_none(monkeypatch):
    gateway = RecordingGateway()
    client = MutationClient(gateway, log=lambda m, level=None: None)
    monkeypatch.setattr(MutationClient, 'waitForAbort',
                        lambda self, timeout=0: True)

    assert client.send('clear') is None


def test_pending_id_is_cleared_after_timeout(monkeypatch):
    # A late real ack after send() returned must find the client idle and
    # be ignored (the re-rendered view is the truth, not a stale reply).
    client = make_client(monkeypatch, RecordingGateway())

    assert client.send('clear') is None
    assert client._pending_id is None
