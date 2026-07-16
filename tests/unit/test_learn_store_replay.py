"""End-to-end learn/store/replay tests against the REAL sparse store (E2).

The design contract's E2 acceptance (EVOLVED §3.2 decision table + the learn
checklist): drive the whole runtime graph through a lifecycle and assert what
lands on DISK, not just what a fake recorded. This is the sibling of
test_session_flow, with one deliberate rig difference: where that suite patches
``runtime.offsets.resolve/store`` to a scripted resolver, THIS suite keeps the
REAL ``OffsetStore`` + ``OffsetTable`` and only points the store at a pytest
``tmp_path`` (via ``xbmcvfs.translatePath``) before constructing the runtime.
The store's atomic writes, corruption quarantine, and lookup/write key rules
are therefore exercised for real — a lookup MISS leaves Kodi untouched and
writes nothing, a learned value round-trips 1 ms verbatim through JSON, and a
replay run applies it with no user input.

Rig conventions copied from test_session_flow: one ``FakeClock`` into every
clock holder, one ``FakeGateway`` into every gateway consumer, settings reads
monkeypatched on the single ``Settings`` adapter, applies captured at the RPC
boundary, and the dispatcher pumped manually with ``run_pending`` (never
started, never slept). Every cadence is derived from the production classes
(``StreamDetector.VERIFY_WINDOW_SECONDS``, ``AdjustmentWatcher.*_SECONDS``) so a
retuned constant moves the tests with the code, never against a hardcoded copy.

User adjustments are driven exactly as production sees them: the gateway's
``Player.AudioDelay`` infolabel is set to a localized delay string and the
clock is walked through the watcher's tick cadence, so a foreign value that
holds for ``QUIESCENCE_SECONDS`` is stored under the D4 write key derived at
store instant. The watcher only stores a CHANGE observed while watching, so
each learn flow first lets a baseline reading settle, THEN dials the new value.
"""

import json
from types import SimpleNamespace

import pytest
import xbmcvfs

from resources.lib.aom.app import events
from resources.lib.aom.app.adjustment_watcher import AdjustmentWatcher
from resources.lib.aom.app.stream_detector import (INFOLABEL_FPS, INFOLABEL_HDR,
                                                   StreamDetector)
from resources.lib.aom.kodi.gui import Gui
from resources.lib.aom.store.offset_store import OffsetStore
from tests.fakes import FakeClock, FakeGateway


# Keyed by the production constants (as test_session_flow does) so a renamed
# infolabel cannot silently degrade the scripted stream while a test stays
# green. The default stream is Dolby Vision / 23.976 fps / TrueHD.
INFOLABELS = {
    INFOLABEL_FPS: '23.976',
    INFOLABEL_HDR: 'dolbyvision',
}
# The single infolabel the watcher polls; named off the class so the poll
# target and the tests read it from one place.
INFO_AUDIO_DELAY = AdjustmentWatcher.INFOLABEL_AUDIO_DELAY


def _make_runtime(monkeypatch, tmp_path, *, per_fps=False, infolabels=None):
    """Build a REAL ServiceRuntime with the store rooted at ``tmp_path``.

    The store path is redirected BEFORE construction (the runtime resolves it
    via ``xbmcvfs.translatePath`` inside ``__init__`` and loads it once), so
    every runtime built in a test — including a fresh "replay" instance —
    speaks to the one on-disk ``offsets.json``.
    """
    # Point the store at the test's tmp file, whatever special:// path the
    # runtime asks to translate. This must land before ServiceRuntime().
    monkeypatch.setattr(xbmcvfs, 'translatePath',
                        lambda _p: str(tmp_path / 'offsets.json'))

    from resources.lib.aom.runtime import ServiceRuntime
    runtime = ServiceRuntime()

    # Deterministic time everywhere: every clock-holding component gets the
    # same FakeClock, so the verify window, the seek quiet window, and the
    # watcher cadences move only when the test advances it.
    clock = FakeClock()
    runtime.dispatcher._clock = clock
    runtime.session_tracker._clock = clock
    runtime.seek_scheduler._clock = clock
    runtime.seek_coordinator._clock = clock
    runtime.notifier._clock = clock
    runtime.adjustment_watcher._clock = clock

    # One scriptable gateway for every consumer (detector reads; applier sets
    # the delay; the coordinator seeks; the watcher polls Player.AudioDelay).
    labels = dict(INFOLABELS)
    if infolabels:
        labels.update(infolabels)
    gateway = FakeGateway(infolabels=labels)
    runtime.detector._gateway = gateway
    runtime.offset_applier._gateway = gateway
    runtime.seek_coordinator._gateway = gateway
    runtime.adjustment_watcher._gateway = gateway

    # Applies captured in the legacy (player_id, ms) shape at the RPC boundary.
    applied = []
    gateway.set_audio_delay = (
        lambda player_id, seconds: applied.append(
            (player_id, round(seconds * 1000))) or True)

    # --- settings seams (the single Settings adapter, shared by all) ----------
    settings = runtime.settings
    # A mutable holder so a test can flip per_fps live (D3: OFF = the `all`
    # key world; ON = exact -> all -> miss).
    per_fps_holder = {'on': per_fps}
    monkeypatch.setattr(settings, 'pause_enabled', lambda: False)
    monkeypatch.setattr(settings, 'per_fps_offsets_enabled',
                        lambda: per_fps_holder['on'])
    monkeypatch.setattr(settings, 'remember_adjustments_enabled', lambda: True)
    monkeypatch.setattr(settings, 'seek_back_config', lambda reason: (False, 0))
    monkeypatch.setattr(settings, 'notify_apply_enabled', lambda: False)
    monkeypatch.setattr(settings, 'notify_learn_enabled', lambda: False)

    # Toasts captured at the notifier's assembly boundary (same seam as
    # test_session_flow): pending/dedupe logic still runs, only the gui read is
    # skipped.
    toasts = []
    monkeypatch.setattr(
        runtime.notifier, '_toast',
        lambda string_id, ms, profile, enabled=None: toasts.append(
            (string_id, ms, profile.describe())))

    # The watcher's typed store signal, captured so learn flows can assert the
    # key/ms that actually landed.
    saved = []
    runtime.dispatcher.subscribe(events.UserOffsetSaved, saved.append)

    return SimpleNamespace(
        runtime=runtime, clock=clock, gateway=gateway, applied=applied,
        toasts=toasts, saved=saved, per_fps=per_fps_holder,
        store_path=tmp_path / 'offsets.json')


@pytest.fixture
def build(monkeypatch, tmp_path):
    """A factory for runtimes sharing one tmp store (fresh per test)."""
    def _factory(*, per_fps=False, infolabels=None):
        return _make_runtime(monkeypatch, tmp_path,
                             per_fps=per_fps, infolabels=infolabels)
    return _factory


# --- pumping helpers (no sleeps; the fake clock is the only time source) -----

def _play(rig):
    """Post PlaybackStarted and drain the cascade (discovery + first apply)."""
    rig.runtime.dispatcher.post(events.PlaybackStarted())
    rig.runtime.dispatcher.run_pending()


def _settle(rig):
    """Let the detector's verification window elapse -> STABLE."""
    rig.clock.advance(StreamDetector.VERIFY_WINDOW_SECONDS)
    rig.runtime.dispatcher.run_pending()


def _quiesce(rig):
    """Walk the clock through the watcher cadence past one quiescence window.

    Steps by ``ACTIVE_TICK_SECONDS`` and pumps each step so the self-scheduled
    WatchTick chain fires at its real cadence (idle poll notices the change,
    then the tightened cadence holds it for QUIESCENCE_SECONDS). The span
    covers the idle tick that first observes the change plus the full
    quiescence hold, with a couple of active ticks of margin.
    """
    span = (AdjustmentWatcher.IDLE_TICK_SECONDS
            + AdjustmentWatcher.QUIESCENCE_SECONDS
            + 4 * AdjustmentWatcher.ACTIVE_TICK_SECONDS)
    end = rig.clock() + span
    while rig.clock() < end:
        rig.clock.advance(AdjustmentWatcher.ACTIVE_TICK_SECONDS)
        rig.runtime.dispatcher.run_pending()


def _seed(store_path, entries, *, video_fps=None):
    """Seed the on-disk store via the REAL OffsetStore (service stopped)."""
    store = OffsetStore(str(store_path))
    for key, ms in entries.items():
        store.set(key, ms, video_fps=video_fps)


def _profiles_on_disk(store_path):
    return json.loads(store_path.read_text(encoding='utf-8'))['profiles']


# --- Scenario 1: first-run silence + miss no-op ------------------------------

def test_first_run_miss_is_silent_and_writes_nothing(build):
    # Empty store: the profile resolves to a MISS, so the applier leaves Kodi's
    # delay untouched (no RPC), toasts nothing, logs the miss exactly once
    # across repeated stabilizations (session.miss_announced dedupe), and —
    # because nothing was learned — never creates offsets.json.
    rig = build()

    # Capture the applier's debug sink to count the miss line.
    misses = []
    rig.runtime.offset_applier._log = misses.append

    _play(rig)
    _settle(rig)
    session = rig.runtime.session_tracker.current

    # Re-stabilize a few times: the miss must not re-log per event.
    for _ in range(3):
        rig.runtime.dispatcher.post(
            events.StreamStabilized(session_id=session.session_id))
        rig.runtime.dispatcher.run_pending()

    assert rig.applied == []                       # no RPC on a miss
    assert rig.toasts == []                         # nothing to announce
    no_stored = [line for line in misses if 'no stored offset' in line]
    assert len(no_stored) == 1                      # one line per distinct chain
    assert not rig.store_path.exists()              # nothing learned -> no file


# --- Scenario 2: learn flow end-to-end ---------------------------------------

def test_learn_flow_writes_verbatim_entry_to_disk(build):
    # Empty store, stream stable, Kodi's delay reads 0 (untouched). The user
    # dials to -115 ms; after quiescence the ON-DISK file gains exactly the
    # `all`-key entry with delay_ms == -115 VERBATIM (1 ms survives the whole
    # pipeline), source 'user', and the exact reported rate as metadata.
    rig = build()

    # Baseline: Kodi's delay is untouched at 0 while the store misses.
    rig.gateway.infolabels[INFO_AUDIO_DELAY] = '0.000 s'
    _play(rig)
    _settle(rig)                                    # adopts baseline 0

    # The user dials the offset; the watcher observes the CHANGE and quiesces.
    rig.gateway.infolabels[INFO_AUDIO_DELAY] = '-0.115 s'
    _quiesce(rig)

    profiles = _profiles_on_disk(rig.store_path)
    assert set(profiles) == {'dolbyvision|all|truehd'}
    entry = profiles['dolbyvision|all|truehd']
    assert entry['delay_ms'] == -115                # verbatim, 1 ms resolution
    assert entry['source'] == 'user'
    assert entry['video_fps'] == 23.976             # exact rate rode along

    assert len(rig.saved) == 1
    assert rig.saved[0].key == 'dolbyvision|all|truehd'
    assert rig.saved[0].ms == -115


# --- Scenario 3: replay ------------------------------------------------------

def test_replay_applies_seeded_offset_on_playback(build, tmp_path):
    # A store seeded (as if by a prior learn) applies on the next playback with
    # no user interaction at all: PlaybackStarted -> adopt -> EXACT hit -> one
    # set_audio_delay of the stored value.
    _seed(tmp_path / 'offsets.json', {'dolbyvision|all|truehd': -115})

    # A runtime built AFTER the seed loads it at construction (single read).
    replay = build()
    _play(replay)

    assert replay.applied == [(1, -115)]            # applied with no user input


# --- Scenario 4: fallback then specialize (per_fps ON, §3.2 worked flow) ------

def test_fallback_then_specialize_per_fps(build, tmp_path):
    # per_fps ON, only an `all`-key entry seeded: a 60 fps stream falls back to
    # it (-125). The user then dials -100; the D4 write key is the fps-specific
    # key, so the exact entry is CREATED while the `all` fallback is untouched.
    # A replay at 60 fps then hits the exact entry (-100).
    _seed(tmp_path / 'offsets.json', {'dolbyvision|all|truehd': -125})

    # Built AFTER the seed so it loads at construction (service stopped seed).
    rig = build(per_fps=True, infolabels={INFOLABEL_FPS: '60'})
    # Kodi echoes the fallback apply back through the infolabel (baseline).
    rig.gateway.infolabels[INFO_AUDIO_DELAY] = '-0.125 s'

    _play(rig)
    assert rig.applied == [(1, -125)]               # fallback applied
    _settle(rig)                                     # self-echo -> baseline -125

    # User dials the fps-specific value; quiescence writes the exact key.
    rig.gateway.infolabels[INFO_AUDIO_DELAY] = '-0.100 s'
    _quiesce(rig)

    profiles = _profiles_on_disk(rig.store_path)
    assert profiles['dolbyvision|60|truehd']['delay_ms'] == -100  # specialized
    assert profiles['dolbyvision|all|truehd']['delay_ms'] == -125  # untouched

    # Replay at 60 fps now hits the exact entry, not the fallback.
    replay = build(per_fps=True, infolabels={INFOLABEL_FPS: '60'})
    _play(replay)
    assert replay.applied == [(1, -100)]


# --- Scenario 5: delete during playback --------------------------------------

def test_delete_mid_session_is_a_miss_no_op(build, tmp_path):
    # A seeded offset applies; the entry is then deleted mid-session (the E4
    # mutation channel's move). The next stabilization re-resolves to a MISS:
    # no additional RPC, and Kodi's delay is NOT reset to 0 (a miss leaves it
    # exactly where the earlier apply put it). A fresh runtime then applies
    # nothing.
    _seed(tmp_path / 'offsets.json', {'dolbyvision|all|truehd': -115})
    rig = build()                                    # loads the seed

    # Kodi echoes the applied value so the watcher self-echoes (stores nothing).
    rig.gateway.infolabels[INFO_AUDIO_DELAY] = '-0.115 s'
    _play(rig)
    _settle(rig)
    assert rig.applied == [(1, -115)]               # seeded offset applied
    session = rig.runtime.session_tracker.current

    # Mutation channel: drop the entry from under the live session.
    assert rig.runtime.store.delete('dolbyvision|all|truehd') is True

    rig.runtime.dispatcher.post(
        events.StreamStabilized(session_id=session.session_id))
    rig.runtime.dispatcher.run_pending()

    # No ADDITIONAL apply, and specifically no reset-to-zero RPC.
    assert rig.applied == [(1, -115)]
    assert (1, 0) not in rig.applied

    # A fresh runtime against the now-empty file applies nothing.
    replay = build()
    _play(replay)
    assert replay.applied == []


# --- Scenario 6: corrupt store at startup ------------------------------------

def test_corrupt_store_survives_startup_and_learns_fresh(build, monkeypatch,
                                                         tmp_path):
    # A garbage offsets.json is quarantined to .bad during construction, the
    # runtime surfaces one gui notification, starts empty, and learning still
    # works end-to-end afterwards (writing a fresh valid file).
    store_path = tmp_path / 'offsets.json'
    store_path.write_bytes(b'this is not valid json at all {{{{')

    # The corruption notification fires DURING construction, so capture at the
    # Gui class before building the runtime.
    notifications = []
    monkeypatch.setattr(
        Gui, 'notification',
        lambda self, message, duration_ms, title=None, icon=None:
            notifications.append((message, duration_ms)))

    rig = build()

    assert notifications                            # user was told
    # ...and told SOMETHING: localized() degrades to '' (the kodistubs
    # Addon does exactly that), and this notice is the only signal the
    # offsets were reset — the English fallback must fill a blank body
    # (E3 review pin).
    assert 'offsets.json.bad' in notifications[0][0]
    assert (tmp_path / 'offsets.json.bad').exists()  # junk quarantined
    assert len(rig.runtime.store) == 0              # started empty
    assert not store_path.exists()                  # the junk file is gone

    # Learning still works: a fresh valid file is written.
    rig.gateway.infolabels[INFO_AUDIO_DELAY] = '0.000 s'
    _play(rig)
    _settle(rig)
    rig.gateway.infolabels[INFO_AUDIO_DELAY] = '-0.115 s'
    _quiesce(rig)

    profiles = _profiles_on_disk(store_path)
    assert profiles['dolbyvision|all|truehd']['delay_ms'] == -115


# --- Scenario 7: teardown phantom stays dead ---------------------------------

def test_teardown_phantom_zero_is_never_stored(build):
    # During a slow stop the delay infolabel can read a parseable 0 while the
    # session is still alive. The watcher's teardown guard re-checks the active
    # player at store time: with the player gone (id -1) the quiesced 0 is
    # discarded, so the on-disk store gains no 0 entry (indeed no file), and no
    # UserOffsetSaved is posted.
    rig = build()

    # Establish a non-zero baseline first, so the phantom 0 reads as a CHANGE.
    rig.gateway.infolabels[INFO_AUDIO_DELAY] = '-0.050 s'
    _play(rig)
    _settle(rig)                                     # adopts baseline -50

    # The teardown phantom: delay flips to 0 AND the player disappears before
    # quiescence completes.
    rig.gateway.infolabels[INFO_AUDIO_DELAY] = '0.000 s'
    rig.gateway.player_id = -1
    _quiesce(rig)

    assert not rig.store_path.exists()              # nothing written
    assert rig.saved == []                          # nothing posted
