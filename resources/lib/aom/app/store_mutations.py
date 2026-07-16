"""Store mutation handler — the service side of the cross-process channel.

The management view runs in the SCRIPT process and must never write the
store file (single-writer doctrine): its mutations travel as
``JSONRPC.NotifyAll`` messages that the monitor bridge turns into typed
``StoreMutationRequested`` events, and THIS component executes them on the
dispatcher thread — the same thread that owns every other store write.

The op whitelist is the security boundary of the channel (P6): only
``delete`` and ``clear`` exist. There is no value field on the event and no
``set`` op, so the channel STRUCTURALLY cannot carry a value write; an
unknown op (or a malformed payload the bridge posted verbatim) is rejected
loudly — one warning line plus a failed ack — never silently dropped.

Every request is acknowledged through the injected ``ack`` callable (the
runtime wires it to ``KodiGateway.notify_all`` under ``ACK_MESSAGE``),
echoing ``request_id`` so the script process can match the reply; no ack
within the script's timeout is its "service not running" signal (D5:
report-only — there is no direct-write fallback).

After a STORE-CHANGING mutation the handler runs ``_store_changed``:
synchronous session-state invalidation (miss dedupe + watcher
observation) plus a typed ``StoreMutated`` the applier consumes as a
resolve moment for the live session (E7, user call 2026-07-16) — deleting
the PLAYING profile's offset takes effect immediately, the marked miss
forcing its promised 0 at the deletion itself. "Store-changing" means the
IN-MEMORY store the live session resolves against: a delete that removed
an entry or a clear with entries, INCLUDING their persist-failed variants
(OffsetStore keeps the in-memory removal and markers when only the disk
write failed — the ack reports the durability truth, the live session
follows the in-memory truth). A missing-key delete, an empty clear, and
refused ops changed nothing and trigger nothing. Nothing HERE touches
Kodi's live delay; the applier owns that reaction, behind its standing
gates.

Protocol constants live here (pure Python) so the monitor bridge and the
script-process client share one definition.
"""

from resources.lib.aom.app import events


def _noop(_message):
    return None


# NotifyAll message names. Kodi surfaces custom messages to monitors as
# 'Other.<message>'; senders/receivers on both processes use these names.
MUTATION_MESSAGE = 'store_mutation'
ACK_MESSAGE = 'store_mutation_ack'

# The complete op vocabulary of the channel (P6: inspection + removal only).
ALLOWED_OPS = ('delete', 'clear')


class StoreMutationHandler:
    """Executes whitelisted cross-process store mutations on the dispatcher."""

    def __init__(self, dispatcher, session_tracker, store, ack, *,
                 log_debug=None, log_warning=None):
        """``store`` is the raw ``OffsetStore`` (mutations bypass the
        ``OffsetTable`` resolve/write-key algebra — they target literal
        keys the view listed). ``ack`` is a REQUIRED callable taking the
        reply payload dict."""
        self._dispatcher = dispatcher
        self._sessions = session_tracker
        self._store = store
        self._ack = ack
        self._log = log_debug or _noop
        self._warn = log_warning or _noop

        dispatcher.subscribe(events.StoreMutationRequested, self._on_requested)

    # -- handler (dispatcher thread) -------------------------------------------

    def _on_requested(self, event):
        if event.op == 'delete':
            reply = self._delete(event.key)
        elif event.op == 'clear':
            reply = self._clear()
        else:
            # The loud rejection (P6): anything outside the whitelist —
            # including a would-be value write or a malformed payload —
            # is named in the log, refused, and acked as failed.
            self._warn(f"AOMe_StoreMutations: rejected op {event.op!r} "
                       f"(allowed: {', '.join(ALLOWED_OPS)})")
            reply = {'ok': False, 'detail': 'rejected'}

        reply['op'] = event.op if event.op in ALLOWED_OPS else None
        reply['request_id'] = event.request_id
        self._ack(reply)

    # -- ops --------------------------------------------------------------------

    def _delete(self, key):
        if not isinstance(key, str) or not key:
            self._warn(f"AOMe_StoreMutations: rejected delete with bad key "
                       f"{key!r}")
            return {'ok': False, 'detail': 'rejected'}
        if self._store.read_only:
            self._warn(f"AOMe_StoreMutations: store is read-only; "
                       f"refusing delete({key!r})")
            return {'ok': False, 'detail': 'read_only'}
        if self._store.get(key) is None:
            # Raced away (or a stale view row): nothing to do, and the ack
            # says so instead of pretending a delete happened.
            return {'ok': False, 'detail': 'missing'}
        if not self._store.delete(key):
            # Present, writable, but the persist failed: the entry would
            # resurrect from disk on the next load — the ack must not
            # claim durability the store does not have. The IN-MEMORY
            # removal stands, though (OffsetStore doctrine), so the live
            # session's store DID change: reconcile it like any other
            # mutation; only durability failed.
            self._store_changed(op='delete', key=key)
            return {'ok': False, 'detail': 'persist_failed'}
        self._log(f"AOMe_StoreMutations: deleted stored offset {key}")
        self._store_changed(op='delete', key=key)
        return {'ok': True, 'detail': 'deleted'}

    def _clear(self):
        if self._store.read_only:
            self._warn("AOMe_StoreMutations: store is read-only; "
                       "refusing clear()")
            return {'ok': False, 'detail': 'read_only'}
        expected = len(self._store)
        count = self._store.clear()
        if count != expected:
            # clear() reports 0 on a persist failure; with entries present
            # that means the file still holds them (see OffsetStore.clear).
            # The in-memory removal stands regardless, so the live store
            # changed: reconcile like any other mutation.
            self._store_changed(op='clear')
            return {'ok': False, 'detail': 'persist_failed', 'count': count}
        self._log(f"AOMe_StoreMutations: cleared {count} stored offset(s)")
        if count:
            # An empty clear changed nothing: no dedupe reset, no event.
            self._store_changed(op='clear')
        return {'ok': True, 'detail': 'cleared', 'count': count}

    # -- internals ---------------------------------------------------------------

    def _store_changed(self, op, key=None):
        """The store changed under the session: reconcile and re-log.

        Three consequences, the first two SYNCHRONOUS on purpose:

        - ``miss_announced`` is cleared (same rule as the watcher's store
          path — E2 review finding, ledgered for these ops: it dedupes the
          applier's "no stored offset" line per consulted chain, and a
          mutation makes any remembered chain stale).
        - The watcher's observation state is cleared (the supersede
          corollary at its root): an in-flight candidate was dialed
          against the store that no longer exists. This CANNOT ride on a
          queued event — the dispatcher fires due timers between queue
          items, so a quiescence-deadline WatchTick can land between this
          handler and any event it posts and store the stale candidate
          under the just-deleted key (resurrecting it, marker discarded
          by set()); and the applier's already-0/failed-RPC reset
          branches deliberately post no DelayReset at all (E7 review,
          cross-file finding). Synchronous assignment on the dispatcher
          thread is race-free by construction.
        - A typed ``StoreMutated`` is posted so the applier re-runs its
          decision for the live session — the deletion acts NOW when its
          profile is playing (E7, user call).
        """
        session = self._sessions.current
        if session is not None:
            session.miss_announced = None
            # The watcher's _clear_observation doctrine, applied inline
            # (baseline and pending fall together — a baseline from the
            # pre-mutation store must not classify post-mutation readings).
            session.watch_pending = None
            session.watch_baseline_ms = None
        self._dispatcher.post(events.StoreMutated(op=op, key=key))
