"""Unit tests for the cross-process store mutation channel (service side).

Two surfaces, tested at their real seams:

* ``StoreMutationHandler`` on a REAL dispatcher + tracker + ``OffsetStore``
  (tmp_path-backed): the whitelist boundary (delete/clear/import ONLY,
  loud rejection of everything else), honest acks for every outcome
  (deleted/missing/read_only/persist_failed/cleared/imported/invalid/
  future), the staged-backup import consuming its staging file whatever
  the outcome, and the ``miss_announced`` dedupe clearing.
* ``MonitorBridge.onNotification``: the sender/message filter and the
  verbatim payload -> typed event decode, including the malformed-JSON
  path that must still surface as a loudly-rejected event.
"""

import pytest

from resources.lib.aome.app import events
from resources.lib.aome.app.dispatcher import Dispatcher
from resources.lib.aome.app.session import SessionTracker
from resources.lib.aome.app.store_mutations import (ACK_MESSAGE, ALLOWED_OPS,
                                                   IMPORT_SUFFIX,
                                                   MUTATION_MESSAGE,
                                                   StoreMutationHandler)
from resources.lib.aome.kodi.monitor_bridge import MonitorBridge
from resources.lib.aome.kodi.settings import ADDON_ID
from resources.lib.aome.store.offset_store import OffsetStore
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
        self.import_path = self.store_path + IMPORT_SUFFIX
        self.store = OffsetStore(self.store_path,
                                 log_debug=self.debug.append,
                                 log_warning=self.warnings.append)
        self.store.load()
        self.handler = StoreMutationHandler(
            self.dispatcher, self.tracker, self.store, self.acks.append,
            import_path=self.import_path,
            log_debug=self.debug.append, log_warning=self.warnings.append)
        # The immediate-reconcile signal: posted only by ops that
        # actually changed the store.
        self.mutated = []
        self.dispatcher.subscribe(events.StoreMutated, self.mutated.append)

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


# --- the whitelist boundary ----------------------------------------------

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
    # Structural pin: the event carries no value field and the whitelist
    # is exactly delete/clear/import — a value write cannot even be
    # EXPRESSED on the wire (import reads values only from the staged
    # backup file the user placed).
    assert ALLOWED_OPS == ('delete', 'clear', 'import')
    assert not hasattr(events.StoreMutationRequested(op='delete'), 'ms')
    assert not hasattr(events.StoreMutationRequested(op='delete'), 'delay_ms')
    assert not hasattr(events.StoreMutationRequested(op='delete'), 'value')
    assert not hasattr(events.StoreMutationRequested(op='import'), 'path')


# --- read-only / persist-failure honesty ---------------------------------------

def test_read_only_store_refuses_all_ops(rig):
    rig.store._read_only = True

    rig.request('delete', key=KEY_A)
    rig.request('clear')
    rig.request('import')

    assert [a['detail'] for a in rig.acks] == ['read_only'] * 3
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


def test_persist_failure_still_reconciles_the_live_store(rig, monkeypatch):
    # OffsetStore keeps the in-memory removal (and markers) when only the
    # disk write fails, and the live session resolves against memory — so
    # a persist-failed delete/clear IS store-changing for the live session
    # and must fire the reconcile signal; the ack alone reports the
    # durability failure.
    monkeypatch.setattr(rig.store, '_persist', lambda: False)

    rig.request('delete', key=KEY_A)
    rig.request('clear')                        # KEY_B still in memory

    assert [(e.op, e.key) for e in rig.mutated] == [
        ('delete', KEY_A), ('clear', None)]


# --- miss_announced clearing ---------------------------------------------------

def test_successful_delete_clears_miss_dedupe_of_live_session(rig):
    rig.post(events.PlaybackStarted())
    session = rig.tracker.current
    session.miss_announced = ('sdr|all|aac',)

    rig.request('delete', key=KEY_A)

    assert session.miss_announced is None


def test_store_changing_ops_clear_watch_state_synchronously(rig):
    # The supersede corollary at its root: an in-flight observation was
    # dialed against the store
    # that no longer exists. The clear cannot ride a queued event — the
    # dispatcher fires due timers between queue items, and the applier's
    # already-0 reset branch posts no event at all — so the handler
    # clears it before returning.
    rig.post(events.PlaybackStarted())
    session = rig.tracker.current
    session.watch_baseline_ms = -115
    session.watch_pending = (0, 123.0)

    rig.request('delete', key=KEY_A)

    assert session.watch_pending is None
    assert session.watch_baseline_ms is None


def test_no_op_requests_leave_watch_state_alone(rig):
    rig.post(events.PlaybackStarted())
    session = rig.tracker.current
    session.watch_baseline_ms = -115
    session.watch_pending = (0, 123.0)

    rig.request('delete', key='not|a|key')

    assert session.watch_pending == (0, 123.0)
    assert session.watch_baseline_ms == -115


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


# --- StoreMutated (the immediate-reconcile signal) -----------------------------

def test_store_changing_ops_post_store_mutated(rig):
    # A delete that removed an entry and a clear with entries both changed
    # the store: the applier's re-resolve trigger must fire (with op/key
    # riding along for the debug trail).
    rig.request('delete', key=KEY_A)
    assert [(e.op, e.key) for e in rig.mutated] == [('delete', KEY_A)]

    rig.request('clear')                        # KEY_B remains: count 1
    assert [(e.op, e.key) for e in rig.mutated] == [
        ('delete', KEY_A), ('clear', None)]


def test_ops_that_change_nothing_post_nothing(rig):
    rig.request('delete', key='not|a|key')      # missing
    rig.request('delete', key=None)             # bad key, rejected
    rig.request('bogus_op', key=KEY_A)          # whitelist rejection
    assert rig.mutated == []

    rig.request('clear')                        # changes the store (2 entries)
    rig.mutated.clear()
    rig.request('clear')                        # empty clear: nothing changed
    assert rig.mutated == []


# --- import (the staged backup restore) ------------------------------------------

import json as _json
import os as _os


def _stage(rig, profiles, version=1, resets=None):
    document = {'version': version, 'profiles': profiles}
    if resets is not None:
        document['resets'] = resets
    with open(rig.import_path, 'w', encoding='utf-8') as handle:
        handle.write(_json.dumps(document))


def test_import_replaces_store_consumes_staging_and_acks_count(rig):
    _stage(rig, {'hlg|all|eac3': {'delay_ms': -75}})
    rig.request('import', request_id='imp')

    assert rig.acks == [{'ok': True, 'detail': 'imported', 'count': 1,
                         'op': 'import', 'request_id': 'imp'}]
    assert set(rig.store.entries()) == {'hlg|all|eac3'}
    # Restore semantics: the dropped keys carry reset markers.
    assert rig.store.reset_pending(KEY_A) is True
    assert rig.store.reset_pending(KEY_B) is True
    # The staging file was consumed.
    assert not _os.path.exists(rig.import_path)
    # Durable: a second store built on the same file agrees.
    reread = OffsetStore(rig.store_path)
    reread.load()
    assert set(reread.entries()) == {'hlg|all|eac3'}
    assert [(e.op, e.key) for e in rig.mutated] == [('import', None)]


def test_import_without_staged_file_is_refused(rig):
    # A spoofed import request (nothing staged) must never clear the store.
    rig.request('import')

    assert rig.acks[0]['ok'] is False
    assert rig.acks[0]['detail'] == 'invalid'
    assert len(rig.store) == 2
    assert rig.mutated == []
    assert rig.warned('unusable staged backup')


def test_import_of_corrupt_staging_is_refused_and_consumed(rig):
    with open(rig.import_path, 'w', encoding='utf-8') as handle:
        handle.write('{nope')
    rig.request('import')

    assert rig.acks[0]['detail'] == 'invalid'
    assert len(rig.store) == 2
    assert rig.mutated == []
    assert not _os.path.exists(rig.import_path)


def test_import_of_empty_backup_is_refused_at_the_choke_point(rig):
    # The view refuses empty backups with friendly wording, but the
    # SERVICE is the security boundary: a hand-made (or truncated-but-
    # valid) empty document must never become a disguised clear-all,
    # whoever sent the request.
    _stage(rig, {})
    rig.request('import')

    assert rig.acks[0]['ok'] is False
    assert rig.acks[0]['detail'] == 'empty'
    assert len(rig.store) == 2
    assert rig.mutated == []
    assert not _os.path.exists(rig.import_path)
    assert rig.warned('empty backup')


def test_import_restores_the_backups_reset_markers(rig):
    # The export is a verbatim file copy, resets section and all; the
    # restore must not silently drop the one section that is not an
    # offset — a pending "expect 0" promise survives the round trip.
    _stage(rig, {'hlg|all|eac3': {'delay_ms': -75}},
           resets=['sdr|all|aac', '', 7])       # scribble dropped, keys kept
    rig.request('import')

    assert rig.acks[0]['ok'] is True
    assert rig.store.reset_pending('sdr|all|aac') is True
    # The live keys the backup dropped are marked too (restore contract).
    assert rig.store.reset_pending(KEY_A) is True


def test_import_of_future_schema_staging_acks_future(rig):
    # The wording split matters to the view: a newer-version backup is not
    # corrupt, it is unimportable by THIS build.
    _stage(rig, {'hlg|all|eac3': {'delay_ms': -75}}, version=99)
    rig.request('import')

    assert rig.acks[0]['detail'] == 'future'
    assert len(rig.store) == 2
    assert not _os.path.exists(rig.import_path)


def test_import_persist_failure_still_reconciles_and_consumes(rig,
                                                              monkeypatch):
    monkeypatch.setattr(rig.store, '_persist', lambda: False)
    _stage(rig, {'hlg|all|eac3': {'delay_ms': -75}})
    rig.request('import')

    assert rig.acks[0] == {'ok': False, 'detail': 'persist_failed',
                           'op': 'import', 'request_id': 'req-1'}
    # In-memory replacement stands (OffsetStore doctrine): reconcile fires.
    assert [(e.op, e.key) for e in rig.mutated] == [('import', None)]
    assert set(rig.store.entries()) == {'hlg|all|eac3'}
    assert not _os.path.exists(rig.import_path)


def test_import_clears_live_session_state_like_other_mutations(rig):
    rig.post(events.PlaybackStarted())
    session = rig.tracker.current
    session.miss_announced = ('sdr|all|aac',)
    session.watch_baseline_ms = -115
    session.watch_pending = (0, 123.0)

    _stage(rig, {'hlg|all|eac3': {'delay_ms': -75}})
    rig.request('import')

    assert session.miss_announced is None
    assert session.watch_pending is None
    assert session.watch_baseline_ms is None


def test_failed_import_leaves_live_session_state_alone(rig):
    rig.post(events.PlaybackStarted())
    session = rig.tracker.current
    session.miss_announced = ('sdr|all|aac',)

    rig.request('import')                        # nothing staged: refused

    assert session.miss_announced == ('sdr|all|aac',)


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


# --- the full wire, both processes -----------------------------------------------

def test_channel_end_to_end_across_both_processes(tmp_path, monkeypatch):
    """client.send -> bridge -> handler -> ack -> client, over the real wire
    shapes.

    Pins the FIELD CONTRACT (op/key/request_id out; ok/detail/op/request_id
    back) across BOTH endpoints at once, so a one-sided rename cannot pass
    while each side's own tests stay green (the payload shape is
    convention, this test is its oracle). The two legs are delivered exactly
    as Kodi would: method 'Other.<message>', data re-serialized as JSON.
    """
    import json
    from resources.lib.aome.kodi.mutation_client import MutationClient

    r = Rig(tmp_path)
    r.store.set(KEY_A, -115)
    bridge = MonitorBridge(r.dispatcher)

    requests = []

    class ClientGateway:
        """The script->service leg, played by Kodi's announce bus."""

        def notify_all(self, sender, message, data):
            requests.append(data)
            bridge.onNotification(sender, 'Other.' + message,
                                  json.dumps(data))
            return True

    client = MutationClient(ClientGateway(), log=lambda m, level=None: None)

    # The service->script leg: the handler's ack callable delivers to the
    # client the same way Kodi would.
    r.handler._ack = lambda payload: client.onNotification(
        ADDON_ID, 'Other.' + ACK_MESSAGE, json.dumps(payload))

    # send() polls between pumps; each poll slice runs the service's
    # dispatcher once (the two processes' interleaving, compressed).
    monkeypatch.setattr(
        MutationClient, 'waitForAbort',
        lambda self, timeout=0: r.dispatcher.run_pending() or False)

    reply = client.send('delete', key=KEY_A)

    assert reply is not None, "the ack never crossed the wire"
    assert reply['ok'] is True
    assert reply['detail'] == 'deleted'
    assert reply['op'] == 'delete'
    assert reply['request_id'] == requests[0]['request_id']
    assert r.store.get(KEY_A) is None
