"""Unit tests for the cross-process store mutation channel (service side).

Two surfaces, tested at their real seams:

* ``StoreMutationHandler`` on a REAL dispatcher + tracker + ``OffsetStore``
  (tmp_path-backed): the whitelist boundary (P6 — delete/clear ONLY, loud
  rejection of everything else), honest acks for every outcome
  (deleted/missing/read_only/persist_failed/cleared), and the
  ``miss_announced`` dedupe clearing the ledgered E2-review rule demands.
* ``MonitorBridge.onNotification``: the sender/message filter and the
  verbatim payload -> typed event decode, including the malformed-JSON
  path that must still surface as a loudly-rejected event.
"""

import pytest

from resources.lib.aom.app import events
from resources.lib.aom.app.dispatcher import Dispatcher
from resources.lib.aom.app.session import SessionTracker
from resources.lib.aom.app.store_mutations import (ACK_MESSAGE, ALLOWED_OPS,
                                                   MUTATION_MESSAGE,
                                                   StoreMutationHandler)
from resources.lib.aom.kodi.monitor_bridge import MonitorBridge
from resources.lib.aom.kodi.settings import ADDON_ID
from resources.lib.aom.store.offset_store import OffsetStore
from tests.fakes import FakeClock


KEY_A = 'dolbyvision|all|truehd'
KEY_B = 'hdr10|all|eac3'


class Rig:
    """Handler assembled on a real store; pump with post()."""

    def __init__(self, tmp_path):
        self.clock = FakeClock()
        self.debug = []
        self.warnings = []
        self.acks = []
        self.dispatcher = Dispatcher(clock=self.clock,
                                     log_error=self.warnings.append,
                                     log_debug=self.debug.append)
        self.tracker = SessionTracker(self.dispatcher, clock=self.clock,
                                      log_debug=self.debug.append)
        self.store_path = str(tmp_path / 'offsets.json')
        self.store = OffsetStore(self.store_path,
                                 log_debug=self.debug.append,
                                 log_warning=self.warnings.append)
        self.store.load()
        self.handler = StoreMutationHandler(
            self.dispatcher, self.tracker, self.store, self.acks.append,
            log_debug=self.debug.append, log_warning=self.warnings.append)

    def post(self, event):
        self.dispatcher.post(event)
        self.dispatcher.run_pending()

    def request(self, op, key=None, request_id='req-1'):
        self.post(events.StoreMutationRequested(
            op=op, key=key, request_id=request_id))

    def warned(self, needle):
        return any(needle in line for line in self.warnings)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.store.set(KEY_A, -115)
    r.store.set(KEY_B, 250)
    r.acks.clear()
    return r


# --- delete -------------------------------------------------------------------

def test_delete_removes_entry_persists_and_acks(rig):
    rig.request('delete', key=KEY_A, request_id='abc')

    assert rig.store.get(KEY_A) is None
    assert rig.store.get(KEY_B) is not None          # only the target died
    assert rig.acks == [{'ok': True, 'detail': 'deleted', 'op': 'delete',
                         'request_id': 'abc'}]
    # Durable: a second store built on the same file agrees.
    reread = OffsetStore(rig.store_path)
    reread.load()
    assert reread.get(KEY_A) is None
    assert reread.get(KEY_B)['delay_ms'] == 250


def test_delete_missing_key_acks_missing_without_disk_touch(rig):
    rig.request('delete', key='sdr|all|aac')
    assert rig.acks[0]['ok'] is False
    assert rig.acks[0]['detail'] == 'missing'
    assert len(rig.store) == 2


@pytest.mark.parametrize("bad_key", [None, '', 7, ['dolbyvision|all|truehd']])
def test_delete_with_non_string_key_is_rejected(rig, bad_key):
    rig.request('delete', key=bad_key)
    assert rig.acks[0]['ok'] is False
    assert rig.acks[0]['detail'] == 'rejected'
    assert rig.warned('rejected delete')
    assert len(rig.store) == 2


# --- clear --------------------------------------------------------------------

def test_clear_empties_store_and_acks_count(rig):
    rig.request('clear', request_id='xyz')

    assert len(rig.store) == 0
    assert rig.acks == [{'ok': True, 'detail': 'cleared', 'count': 2,
                         'op': 'clear', 'request_id': 'xyz'}]
    reread = OffsetStore(rig.store_path)
    reread.load()
    assert len(reread) == 0


def test_clear_on_empty_store_is_success_with_zero_count(rig):
    rig.store.clear()
    rig.acks.clear()
    rig.request('clear')
    assert rig.acks[0]['ok'] is True
    assert rig.acks[0]['count'] == 0


# --- the whitelist boundary (P6) ----------------------------------------------

@pytest.mark.parametrize("bad_op", [
    'set', 'write', 'update', 'DELETE', None, 7, {'op': 'delete'},
])
def test_non_whitelisted_ops_are_rejected_loudly(rig, bad_op):
    rig.request(bad_op, key=KEY_A)

    assert len(rig.store) == 2                       # store untouched
    assert rig.acks[0]['ok'] is False
    assert rig.acks[0]['detail'] == 'rejected'
    assert rig.acks[0]['op'] is None                 # never echoes junk ops
    assert rig.warned('rejected op')


def test_the_channel_has_no_value_write_op(rig):
    # P6 structural pin: the event carries no value field and the whitelist
    # is exactly delete/clear — a value write cannot even be EXPRESSED.
    assert ALLOWED_OPS == ('delete', 'clear')
    assert not hasattr(events.StoreMutationRequested(op='delete'), 'ms')
    assert not hasattr(events.StoreMutationRequested(op='delete'), 'delay_ms')
    assert not hasattr(events.StoreMutationRequested(op='delete'), 'value')


# --- read-only / persist-failure honesty ---------------------------------------

def test_read_only_store_refuses_both_ops(rig):
    rig.store._read_only = True

    rig.request('delete', key=KEY_A)
    rig.request('clear')

    assert [a['detail'] for a in rig.acks] == ['read_only', 'read_only']
    assert all(a['ok'] is False for a in rig.acks)
    assert len(rig.store) == 2


def test_persist_failure_acks_persist_failed(rig, monkeypatch):
    monkeypatch.setattr(rig.store, '_persist', lambda: False)

    rig.request('delete', key=KEY_A)
    assert rig.acks[0] == {'ok': False, 'detail': 'persist_failed',
                           'op': 'delete', 'request_id': 'req-1'}

    rig.acks.clear()
    rig.request('clear')
    assert rig.acks[0]['ok'] is False
    assert rig.acks[0]['detail'] == 'persist_failed'


# --- miss_announced clearing (E2-review rule, ledgered for these ops) -----------

def test_successful_delete_clears_miss_dedupe_of_live_session(rig):
    rig.post(events.PlaybackStarted())
    session = rig.tracker.current
    session.miss_announced = ('sdr|all|aac',)

    rig.request('delete', key=KEY_A)

    assert session.miss_announced is None


def test_successful_clear_clears_miss_dedupe_of_live_session(rig):
    rig.post(events.PlaybackStarted())
    session = rig.tracker.current
    session.miss_announced = ('sdr|all|aac',)

    rig.request('clear')

    assert session.miss_announced is None


def test_failed_mutations_leave_miss_dedupe_alone(rig):
    rig.post(events.PlaybackStarted())
    session = rig.tracker.current
    session.miss_announced = ('sdr|all|aac',)

    rig.request('delete', key='no|such|key')        # missing
    rig.request('set', key=KEY_A)                   # rejected

    assert session.miss_announced == ('sdr|all|aac',)


def test_mutation_without_a_session_does_not_crash(rig):
    assert rig.tracker.current is None
    rig.request('delete', key=KEY_A)
    assert rig.acks[0]['ok'] is True


# --- MonitorBridge.onNotification ----------------------------------------------

class RecordingDispatcher:
    def __init__(self):
        self.posted = []

    def post(self, event):
        self.posted.append(event)


@pytest.fixture
def bridge_rig():
    dispatcher = RecordingDispatcher()
    return MonitorBridge(dispatcher), dispatcher


def test_bridge_decodes_mutation_payload(bridge_rig):
    bridge, dispatcher = bridge_rig
    bridge.onNotification(
        ADDON_ID, 'Other.' + MUTATION_MESSAGE,
        '{"op": "delete", "key": "dolbyvision|all|truehd", '
        '"request_id": "r9"}')

    assert dispatcher.posted == [events.StoreMutationRequested(
        op='delete', key='dolbyvision|all|truehd', request_id='r9')]


def test_bridge_ignores_other_senders_and_messages(bridge_rig):
    bridge, dispatcher = bridge_rig
    bridge.onNotification('xbmc', 'Player.OnPlay', '{}')
    bridge.onNotification('some.other.addon', 'Other.' + MUTATION_MESSAGE,
                          '{"op": "clear"}')
    # The service's own acks share the sender but not the message: the
    # bridge must not loop them back into the dispatcher.
    bridge.onNotification(ADDON_ID, 'Other.' + ACK_MESSAGE,
                          '{"ok": true, "request_id": "r9"}')

    assert dispatcher.posted == []


@pytest.mark.parametrize("data", ['not json {{{', '"a string"', '[1,2]'])
def test_bridge_posts_malformed_payloads_for_loud_rejection(bridge_rig, data):
    bridge, dispatcher = bridge_rig
    bridge.onNotification(ADDON_ID, 'Other.' + MUTATION_MESSAGE, data)

    assert dispatcher.posted == [events.StoreMutationRequested(op=None)]


def test_bridge_treats_empty_data_as_empty_request(bridge_rig):
    bridge, dispatcher = bridge_rig
    bridge.onNotification(ADDON_ID, 'Other.' + MUTATION_MESSAGE, '')

    # Decodes to {} -> op None -> rejected loudly downstream.
    assert dispatcher.posted == [events.StoreMutationRequested(
        op=None, key=None, request_id=None)]
