"""Unit tests for the script-process mutation channel client.

``xbmc.executeJSONRPC`` is monkeypatched to capture the broadcast request
and (per test) synthesize the service's ack by calling ``onNotification``
directly — the same delivery path Kodi's announce thread uses.
``waitForAbort`` is patched to advance without real sleeping so the
timeout path runs instantly.
"""

import json

import pytest
import xbmc

from resources.lib.aom.app.store_mutations import (ACK_MESSAGE,
                                                   MUTATION_MESSAGE)
from resources.lib.aom.kodi.mutation_client import MutationClient
from resources.lib.aom.kodi.settings import ADDON_ID


ACK_METHOD = 'Other.' + ACK_MESSAGE


@pytest.fixture
def client(monkeypatch):
    logs = []
    c = MutationClient(log=lambda message, level=None: logs.append(message))
    c.test_logs = logs
    # Never really block: abort is never requested in these tests.
    monkeypatch.setattr(MutationClient, 'waitForAbort',
                        lambda self, timeout=0: False)
    return c


def _rpc_ok(monkeypatch, sent):
    def fake_rpc(raw):
        sent.append(json.loads(raw))
        return json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': 'OK'})
    monkeypatch.setattr(xbmc, 'executeJSONRPC', fake_rpc)


def test_send_broadcasts_request_and_returns_matching_ack(client, monkeypatch):
    sent = []

    def fake_rpc(raw):
        request = json.loads(raw)
        sent.append(request)
        # The service answers instantly: echo the request_id on the ack.
        request_id = request['params']['data']['request_id']
        client.onNotification(ADDON_ID, ACK_METHOD, json.dumps(
            {'ok': True, 'detail': 'deleted', 'request_id': request_id}))
        return json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': 'OK'})

    monkeypatch.setattr(xbmc, 'executeJSONRPC', fake_rpc)

    reply = client.send('delete', key='dolbyvision|all|truehd')

    assert reply['ok'] is True and reply['detail'] == 'deleted'
    params = sent[0]['params']
    assert sent[0]['method'] == 'JSONRPC.NotifyAll'
    assert params['sender'] == ADDON_ID
    assert params['message'] == MUTATION_MESSAGE
    assert params['data']['op'] == 'delete'
    assert params['data']['key'] == 'dolbyvision|all|truehd'
    assert params['data']['request_id']            # non-empty match token


def test_clear_request_omits_the_key_field(client, monkeypatch):
    sent = []
    _rpc_ok(monkeypatch, sent)

    client.send('clear')

    assert 'key' not in sent[0]['params']['data']


def test_timeout_without_ack_returns_none(client, monkeypatch):
    sent = []
    _rpc_ok(monkeypatch, sent)

    assert client.send('delete', key='k|all|a') is None
    assert any('No ack' in line for line in client.test_logs)


def test_stale_ack_from_an_earlier_request_is_ignored(client, monkeypatch):
    sent = []
    _rpc_ok(monkeypatch, sent)

    # An ack for some OTHER request id arrives while waiting: must not
    # satisfy this send.
    original = MutationClient.waitForAbort

    def deliver_stale_then_wait(self, timeout=0):
        self.onNotification(ADDON_ID, ACK_METHOD, json.dumps(
            {'ok': True, 'request_id': 'stale-previous-request'}))
        return False

    monkeypatch.setattr(MutationClient, 'waitForAbort',
                        deliver_stale_then_wait)

    assert client.send('clear') is None


def test_foreign_and_malformed_acks_are_ignored(client):
    client._pending_id = 'live-request'

    client.onNotification('some.other.addon', ACK_METHOD,
                          '{"request_id": "live-request", "ok": true}')
    client.onNotification(ADDON_ID, 'Other.' + MUTATION_MESSAGE,
                          '{"request_id": "live-request", "ok": true}')
    client.onNotification(ADDON_ID, ACK_METHOD, 'not json {{{')
    client.onNotification(ADDON_ID, ACK_METHOD, '[1, 2]')

    assert client._reply is None


def test_rpc_error_response_returns_none_without_waiting(client, monkeypatch):
    monkeypatch.setattr(
        xbmc, 'executeJSONRPC',
        lambda raw: json.dumps({'jsonrpc': '2.0', 'id': 1,
                                'error': {'code': -32602}}))

    assert client.send('delete', key='k|all|a') is None


def test_abort_during_wait_returns_none(client, monkeypatch):
    sent = []
    _rpc_ok(monkeypatch, sent)
    monkeypatch.setattr(MutationClient, 'waitForAbort',
                        lambda self, timeout=0: True)

    assert client.send('clear') is None
