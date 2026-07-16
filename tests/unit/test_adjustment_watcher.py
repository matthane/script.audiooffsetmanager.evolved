"""Unit tests for aom.app.adjustment_watcher (AdjustmentWatcher).

Driven exactly like test_seek_scheduler / test_stream_detector: a FakeClock
plus manually pumped Dispatcher, a real SessionTracker (subscribed FIRST so
the watcher always sees a live session), a scriptable FakeGateway (the audio
delay is set via ``gateway.infolabels['Player.AudioDelay']``), the shared
FakeFacade (eligibility reads) + FakeOffsetTable (the store adapter fake),
and a real AdjustmentWatcher. UserOffsetSaved posts are collected off the bus.

E2: stores land in the sparse store under the D4 write key (derived at store
instant from the live profile + per_fps toggle); eligibility is
remember-adjustments + not-paused (the classic per-HDR/unknown-axis gates
died with their features); the settings-dialog store deferral is DELETED —
offsets are not settings, so the dialog cannot clobber them.

Timing facts the tests rely on (all derived from the class constants):

* On ProfileChanged/SettingsChanged an eligible session schedules ONE
  WatchTick at IDLE_TICK_SECONDS under a single key-replaced key, so the poll
  chain is idempotent — there is only ever one live tick.
* A foreign change (away from the baseline) tightens the cadence to
  ACTIVE_TICK_SECONDS and must HOLD unchanged for QUIESCENCE_SECONDS before it
  is stored. Driving QUIESCENCE_STEPS (= QUIESCENCE/ACTIVE) ACTIVE ticks from
  the first observation reaches the store.
* The baseline is the last value ACCOUNTED FOR (ours, adopted, or stored).
  Only a change away from it while watching becomes an adjustment; the first
  non-ours value seen is adopted silently, never stored.
"""

import pytest

from resources.lib.aom.app import events
from resources.lib.aom.app.adjustment_watcher import AdjustmentWatcher
from resources.lib.aom.app.dispatcher import Dispatcher
from resources.lib.aom.app.session import SessionTracker
from resources.lib.aom.domain.profile import StreamProfile
from tests.fakes import FakeClock, FakeFacade, FakeGateway, FakeOffsetTable


# Timing constants derive from the watcher so a retune cannot leave these
# tests green-but-wrong against stale cadences.
IDLE = AdjustmentWatcher.IDLE_TICK_SECONDS
ACTIVE = AdjustmentWatcher.ACTIVE_TICK_SECONDS
QUIET = AdjustmentWatcher.QUIESCENCE_SECONDS
TICK_KEY = AdjustmentWatcher._TICK_KEY
AUDIO_DELAY = AdjustmentWatcher.INFOLABEL_AUDIO_DELAY
# ACTIVE ticks from the first foreign observation to the store (inclusive of
# the tick that crosses the quiescence threshold).
QUIESCENCE_STEPS = int(round(QUIET / ACTIVE))

# The D4 write keys for the default rig (per_fps False -> the all level).
KEY_A = 'dolbyvision|all|truehd'
KEY_B = 'hdr10|all|eac3'


def make_profile(hdr_type='dolbyvision', audio_format='truehd',
                 video_fps=23.976, player_id=1):
    """A complete, eligible profile (writes dolbyvision|all|truehd)."""
    return StreamProfile(hdr_type=hdr_type, audio_format=audio_format,
                         video_fps=video_fps, player_id=player_id,
                         audio_channels=8)


class Rig:
    """The watcher assembled on fakes; pump with post/advance."""

    def __init__(self):
        self.clock = FakeClock()
        self.errors = []
        self.debug = []
        self.warnings = []
        self.dispatcher = Dispatcher(clock=self.clock,
                                     log_error=self.errors.append,
                                     log_debug=self.debug.append)
        # Tracker subscribes lifecycle FIRST so the watcher always sees a live
        # session (dispatch follows subscription order).
        self.tracker = SessionTracker(self.dispatcher, clock=self.clock,
                                      log_debug=self.debug.append)
        self.gateway = FakeGateway()
        self.facade = FakeFacade()
        self.offset_table = FakeOffsetTable()
        self.watcher = AdjustmentWatcher(
            self.dispatcher, self.tracker, self.gateway, self.facade,
            self.offset_table, clock=self.clock, log_debug=self.debug.append,
            log_warning=self.warnings.append)
        self.saved = []
        self.dispatcher.subscribe(events.UserOffsetSaved, self.saved.append)

    # -- pumping ----------------------------------------------------------------

    def post(self, event):
        self.dispatcher.post(event)
        self.dispatcher.run_pending()

    def advance(self, seconds):
        self.clock.advance(seconds)
        self.dispatcher.run_pending()

    # -- convenience ------------------------------------------------------------

    @property
    def session(self):
        return self.tracker.current

    @property
    def watching(self):
        """True while a live poll tick is scheduled (the chain is running)."""
        return TICK_KEY in self.dispatcher._active_keys

    def set_delay(self, delay_str):
        self.gateway.infolabels[AUDIO_DELAY] = delay_str

    def start(self, profile, applied=None):
        """Begin a session and hand it a profile (the detector isn't in the rig)."""
        self.post(events.PlaybackStarted())
        session = self.session
        session.profile = profile
        session.applied = applied

    def arm(self):
        """Kick the watch chain for the current session (schedules the tick)."""
        self.post(events.ProfileChanged(session_id=self.session.session_id))

    def begin(self, profile, baseline_delay='0.000 s', applied=None):
        """Start + arm + drive the first idle tick so a baseline is adopted."""
        self.start(profile, applied=applied)
        self.set_delay(baseline_delay)
        self.arm()
        self.advance(IDLE)   # first idle tick adopts the baseline

    def observe_foreign(self, delay_str):
        """Set a foreign value and fire the pending idle tick (opens pending)."""
        self.set_delay(delay_str)
        self.advance(IDLE)

    def hold_to_quiescence(self):
        """Drive ACTIVE ticks until the pending value crosses quiescence."""
        for _ in range(QUIESCENCE_STEPS):
            self.advance(ACTIVE)

    def logged(self, needle):
        return any(needle in line for line in self.debug)


@pytest.fixture
def rig():
    return Rig()


# ============================================================================
# Self-echo and first-observation adoption (the "never store a found value")
# ============================================================================

class TestBaselineAdoption:

    def test_self_echo_is_ignored(self, rig):
        # Our own applied value echoing back through the infolabel is never a
        # user adjustment: baseline tracks it, nothing is stored.
        profile = make_profile()
        rig.start(profile, applied=(KEY_A, -125))
        rig.set_delay('-0.125 s')
        rig.arm()

        for _ in range(4):
            rig.advance(IDLE)

        assert rig.offset_table.stored == []
        assert rig.saved == []
        assert rig.session.watch_baseline_ms == -125
        assert rig.session.watch_pending is None

    def test_first_observation_is_adopted_not_stored(self, rig):
        # applied is None and the player already reports a delay: it must be a
        # failed-apply leftover / pre-existing state, so adopt it as baseline
        # SILENTLY and never write it over the user's configured offset.
        profile = make_profile()
        rig.start(profile, applied=None)
        rig.set_delay('-0.100 s')
        rig.arm()

        rig.advance(IDLE)
        assert rig.session.watch_baseline_ms == -100
        assert rig.offset_table.stored == []
        assert rig.saved == []

        rig.advance(IDLE)                 # still the same value -> nothing
        assert rig.offset_table.stored == []
        assert rig.saved == []


# ============================================================================
# Quiescence: the store decision
# ============================================================================

class TestQuiescence:

    def test_held_change_stores_exactly_once(self, rig):
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')   # baseline 0
        assert rig.session.watch_baseline_ms == 0

        rig.observe_foreign('-0.050 s')                # opens the pending window
        assert rig.session.watch_pending is not None
        assert rig.offset_table.stored == []                 # not yet quiesced

        rig.hold_to_quiescence()                       # holds >= QUIET

        assert rig.offset_table.stored == [(KEY_A, -50)]
        assert len(rig.saved) == 1
        saved = rig.saved[0]
        assert saved.session_id == rig.session.session_id
        assert saved.profile == profile
        assert saved.ms == -50
        assert saved.key == KEY_A                      # the resolved D4 key
        assert rig.session.applied == (KEY_A, -50)
        assert rig.session.watch_baseline_ms == -50
        assert rig.session.watch_pending is None

        # Further idle ticks (now a self-echo of the stored value) do nothing.
        rig.advance(IDLE)
        rig.advance(IDLE)
        assert rig.offset_table.stored == [(KEY_A, -50)]
        assert len(rig.saved) == 1

    def test_adjust_back_before_quiescence_stores_nothing(self, rig):
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')

        rig.observe_foreign('-0.050 s')                # pending opens on -50
        rig.advance(ACTIVE)                            # one active tick, still -50
        assert rig.session.watch_pending is not None

        rig.set_delay('0.000 s')                       # dialed back before quiescence
        rig.advance(ACTIVE)

        assert rig.offset_table.stored == []
        assert rig.saved == []
        assert rig.session.watch_pending is None
        assert rig.session.watch_baseline_ms == 0

    def test_moving_value_stores_only_the_settled_one(self, rig):
        # -25 for half a second, then -50: only -50 stores, and only after IT
        # holds a full quiescence window (the pending restarts on the change).
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')

        rig.observe_foreign('-0.025 s')                # pending on -25
        rig.advance(ACTIVE)                            # -25 held 0.5s total
        rig.advance(ACTIVE)
        assert rig.offset_table.stored == []

        rig.set_delay('-0.050 s')                      # value moves before -25 quiesced
        rig.advance(ACTIVE)                            # pending restarts on -50
        assert rig.session.watch_pending[0] == -50
        assert rig.offset_table.stored == []

        rig.hold_to_quiescence()                       # -50 now holds >= QUIET

        assert rig.offset_table.stored == [(KEY_A, -50)]
        assert [s.ms for s in rig.saved] == [-50]

    def test_store_lands_under_the_current_profile(self, rig):
        # Pending opens under profile A; profile is replaced with B before
        # quiescence completes. The store and event carry B's key/profile —
        # the write key is derived FRESH at store time on the dispatcher
        # thread (closing the legacy adopt-vs-store interleaving).
        profile_a = make_profile(hdr_type='dolbyvision', audio_format='truehd')
        profile_b = make_profile(hdr_type='hdr10', audio_format='eac3')

        rig.begin(profile_a, baseline_delay='0.000 s')
        rig.observe_foreign('-0.050 s')                # pending under A
        rig.session.profile = profile_b                # swap before quiescence

        rig.hold_to_quiescence()

        assert rig.offset_table.stored == [(KEY_B, -50)]
        assert len(rig.saved) == 1
        assert rig.saved[0].profile == profile_b
        assert rig.saved[0].ms == -50
        assert rig.saved[0].key == KEY_B

    def test_write_key_follows_the_live_per_fps_toggle(self, rig):
        # D4: the key is derived at STORE INSTANT from profile + toggle. A
        # toggle flipped mid-observation writes the specific key, not the
        # all key that was in force when the pending opened.
        profile = make_profile(video_fps=23.976)
        rig.begin(profile, baseline_delay='0.000 s')

        rig.observe_foreign('-0.050 s')                # pending under all-key world
        rig.offset_table.per_fps = True                # toggle flips mid-wait
        rig.hold_to_quiescence()

        assert rig.offset_table.stored == [('dolbyvision|23|truehd', -50)]
        assert rig.saved[0].key == 'dolbyvision|23|truehd'

    def test_our_own_apply_during_pending_is_self_echo(self, rig):
        # A foreign value is pending; then session.applied catches up to that
        # value (a mid-play automatic apply): the next tick treats it as our
        # own echo — baseline refresh, pending cleared, no store.
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')
        rig.observe_foreign('-0.050 s')
        assert rig.session.watch_pending is not None

        rig.session.applied = (KEY_A, -50)
        rig.advance(ACTIVE)

        assert rig.offset_table.stored == []
        assert rig.saved == []
        assert rig.session.watch_pending is None
        assert rig.session.watch_baseline_ms == -50

    def test_observed_equals_already_stored_value_stores_nothing(self, rig):
        # The user dials to a value that is ALREADY stored at the write key:
        # no store call, no event — baseline simply adopts it.
        profile = make_profile()
        rig.offset_table.offsets[KEY_A] = -50
        rig.begin(profile, baseline_delay='0.000 s')

        rig.observe_foreign('-0.050 s')
        rig.hold_to_quiescence()

        assert rig.offset_table.stored == []
        assert rig.saved == []
        assert rig.session.watch_baseline_ms == -50

    def test_fallback_valued_nudge_writes_the_specific_key(self, rig):
        # per_fps ON, only the all-level taught: the user dials to a NEW value
        # while playing 60fps — the write lands on the SPECIFIC key (D4),
        # leaving the all entry untouched (the §3.2 worked flow).
        rig.offset_table.per_fps = True
        rig.offset_table.offsets['dolbyvision|all|truehd'] = -125
        profile = make_profile(video_fps=60.0)
        rig.begin(profile, baseline_delay='-0.125 s',
                  applied=('dolbyvision|all|truehd', -125))

        rig.observe_foreign('-0.100 s')
        rig.hold_to_quiescence()

        assert rig.offset_table.stored == [('dolbyvision|60|truehd', -100)]
        assert rig.offset_table.offsets['dolbyvision|all|truehd'] == -125


# ============================================================================
# Store-path edge cases: incomplete profile, store failure, teardown phantom
# ============================================================================

class TestStorePathGuards:

    def test_incomplete_profile_at_store_time_stores_nothing(self, rig):
        # Watched (eligibility no longer axis-gates) but audio unknown: the
        # store path re-validates the WHOLE profile, refuses to persist an
        # incomplete key, and adopts the value into the baseline so it stops
        # being chased.
        profile = make_profile(audio_format='unknown')
        rig.begin(profile, baseline_delay='0.000 s')
        assert rig.watching                            # watched regardless

        rig.observe_foreign('-0.050 s')
        rig.hold_to_quiescence()

        assert rig.offset_table.stored == []
        assert rig.saved == []
        assert rig.session.watch_baseline_ms == -50

    def test_missing_fps_at_store_time_stores_nothing(self, rig):
        # Same guard for the fps axis (completeness requires a parsed rate).
        profile = make_profile(video_fps=None)
        rig.begin(profile, baseline_delay='0.000 s')

        rig.observe_foreign('-0.050 s')
        rig.hold_to_quiescence()

        assert rig.offset_table.stored == []
        assert rig.saved == []
        assert rig.session.watch_baseline_ms == -50

    def test_store_proceeds_with_settings_dialog_open(self, rig):
        # DELIBERATE behavior change from classic: offsets live in the store
        # file, not settings.xml, so the settings dialog's save-on-close
        # cannot clobber them — there is nothing to defer for (P4: the
        # hazard class is deleted, not managed).
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')

        rig.gateway.settings_dialog = True
        rig.observe_foreign('-0.050 s')
        rig.hold_to_quiescence()

        assert rig.offset_table.stored == [(KEY_A, -50)]
        assert len(rig.saved) == 1

    def test_player_gone_at_store_time_discards_the_observation(self, rig):
        # Regression pin for the 2.0.0~beta1 teardown-phantom field bug
        # (CoreELEC/PM4K, 2026-07-15): during a slow stop the delay infolabel
        # reads a parseable 0 while the session is still alive; quiescence
        # elapsed 99ms before PlaybackStopped and 0 was stored over the
        # user's saved offset. The store path must re-check player liveness
        # and discard the WHOLE observation chain (baseline included) when
        # the player is already gone.
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.020 s')

        rig.observe_foreign('0.000 s')                 # teardown phantom
        rig.gateway.player_id = -1                     # player dies mid-wait
        rig.hold_to_quiescence()

        assert rig.offset_table.stored == []
        assert rig.saved == []
        assert rig.session.watch_pending is None
        assert rig.session.watch_baseline_ms is None   # chain fully cleared
        assert rig.logged('no active player at store time')

    def test_quiescence_outruns_measured_teardown_windows(self, rig):
        # The longest field-measured phantom window (infolabel 0 with the
        # session alive) was 1.15s; quiescence must exceed it with margin so
        # the session dies before a phantom can quiesce.
        assert AdjustmentWatcher.QUIESCENCE_SECONDS >= 2.0

    def test_store_failure_warns_keeps_baseline_and_retries(self, rig):
        profile = make_profile()
        rig.offset_table.store_ok = False
        rig.begin(profile, baseline_delay='0.000 s')

        rig.observe_foreign('-0.050 s')
        rig.hold_to_quiescence()                       # store attempt fails

        assert rig.offset_table.stored == []
        assert rig.saved == []
        assert rig.session.watch_baseline_ms == 0      # NOT updated on failure
        assert any('failed to store' in m for m in rig.warnings)

        # The value is still foreign, so a later cycle retries and succeeds.
        rig.offset_table.store_ok = True
        rig.observe_foreign('-0.050 s')                # re-opens pending on -50
        rig.hold_to_quiescence()

        assert rig.offset_table.stored == [(KEY_A, -50)]
        assert len(rig.saved) == 1


# ============================================================================
# Eligibility gating and the poll chain
# ============================================================================

class TestEligibilityAndChain:

    def test_ineligible_stops_chain_and_re_enable_resumes(self, rig):
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')
        assert rig.watching

        # Disable learning + SettingsChanged -> the chain is cancelled.
        rig.facade.remember_adjustments = False
        rig.post(events.SettingsChanged())
        assert not rig.watching

        # Re-enable + SettingsChanged -> the chain resumes.
        rig.facade.remember_adjustments = True
        rig.post(events.SettingsChanged())
        assert rig.watching

        # A due tick that finds learning disabled reschedules NOTHING.
        rig.facade.remember_adjustments = False
        rig.advance(IDLE)                              # the pending tick fires
        assert not rig.watching
        assert rig.logged('no longer eligible')

    def test_read_only_store_is_not_watched(self, rig):
        # E2 review finding: a permanently unwritable store (newer-schema
        # file after a downgrade) must stop the learn loop outright, not
        # re-detect and re-fail the same adjustment every quiescence cycle.
        rig.offset_table.read_only = True
        rig.start(make_profile())
        rig.set_delay('-0.050 s')
        rig.arm()
        assert not rig.watching

    def test_successful_store_clears_the_miss_dedupe(self, rig):
        # E2 review finding: the applier's once-per-chain miss log dedupes
        # on session.miss_announced; a store WRITE invalidates that chain
        # (delete -> re-teach -> delete must re-log its miss).
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')
        rig.session.miss_announced = (KEY_A,)          # a prior miss episode

        rig.observe_foreign('-0.050 s')
        rig.hold_to_quiescence()

        assert rig.offset_table.stored == [(KEY_A, -50)]
        assert rig.session.miss_announced is None      # chain invalidated

    def test_paused_is_not_watched(self, rig):
        # The global pause (D9) stops learning too: a paused addon must not
        # write store entries behind the user's back.
        profile = make_profile()
        rig.facade.paused = True
        rig.start(profile)
        rig.set_delay('-0.050 s')
        rig.arm()
        assert not rig.watching

    def test_incomplete_profile_is_still_watched(self, rig):
        # Eligibility no longer axis-gates: an audio-unknown stream is
        # watched (classic parity) — the STORE path refuses persistence.
        rig.start(make_profile(audio_format='unknown'))
        rig.set_delay('-0.050 s')
        rig.arm()
        assert rig.watching

    def test_profile_adoption_clears_the_observation(self, rig):
        # A (re)adoption makes any in-flight candidate ambiguous: the pending
        # value was dialed against the PREVIOUS profile, so a real
        # ProfileChanged drops pending AND baseline — even if the new
        # profile's apply failed (session.applied unchanged), the old value
        # is re-adopted as baseline, never stored under the new key (the
        # adopt-vs-store hazard, closed structurally).
        profile_a = make_profile(hdr_type='dolbyvision', audio_format='truehd')
        profile_b = make_profile(hdr_type='hdr10', audio_format='eac3')
        rig.begin(profile_a, baseline_delay='0.000 s')

        rig.observe_foreign('-0.050 s')                # pending under A
        assert rig.session.watch_pending is not None

        rig.session.profile = profile_b                # detector adopts B...
        rig.arm()                                      # ...and posts ProfileChanged
        assert rig.session.watch_pending is None       # candidate dropped
        assert rig.session.watch_baseline_ms is None   # baseline dropped too

        rig.hold_to_quiescence()                       # old cadence plays out
        rig.advance(IDLE)                              # fresh first observation
        assert rig.offset_table.stored == []           # nothing ever stored
        assert rig.saved == []
        assert rig.session.watch_baseline_ms == -50    # re-adopted, not stored

    def test_baseline_cleared_when_watching_stops(self, rig):
        # Only a change observed WHILE watching is an adjustment: a delay
        # changed during a learning-disabled gap must be re-adopted as the
        # baseline on re-enable, never stored against the stale baseline
        # (fresh-state parity with a restarted legacy monitor).
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')
        assert rig.session.watch_baseline_ms == 0

        rig.facade.remember_adjustments = False
        rig.post(events.SettingsChanged())             # chain stops
        assert rig.session.watch_baseline_ms is None   # observation state gone

        rig.set_delay('-0.080 s')                      # changed while not watching
        rig.facade.remember_adjustments = True
        rig.post(events.SettingsChanged())             # chain resumes
        rig.advance(IDLE)                              # first tick re-adopts
        assert rig.session.watch_baseline_ms == -80
        assert rig.offset_table.stored == []
        assert rig.saved == []

        # A change observed while watching still stores normally.
        rig.observe_foreign('-0.050 s')
        rig.hold_to_quiescence()
        assert rig.offset_table.stored == [(KEY_A, -50)]

    def test_unparseable_infolabel_is_tolerated(self, rig):
        # An empty/garbled audio-delay reading keeps the chain alive (retry on
        # the idle cadence) and stores nothing.
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')

        rig.set_delay('')                              # unparseable
        rig.advance(IDLE)
        assert rig.watching                            # chain still running
        assert rig.offset_table.stored == []
        rig.advance(IDLE)
        assert rig.watching
        assert rig.saved == []


# ============================================================================
# Cadence and session turnover
# ============================================================================

class TestCadenceAndLifecycle:

    def test_tick_cadence_idle_vs_active(self, rig):
        # Idle cadence is IDLE (0.25s is not enough to fire the poll); active
        # cadence (while pending) is ACTIVE (a 0.25s advance fires a poll).
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')

        # Idle: a foreign value set, but < IDLE elapsed -> no poll, no pending.
        rig.set_delay('-0.050 s')
        rig.advance(ACTIVE)                            # 0.25 < IDLE
        assert rig.session.watch_pending is None
        rig.advance(IDLE - ACTIVE)                     # reaches IDLE -> polls
        assert rig.session.watch_pending is not None
        assert rig.session.watch_pending[0] == -50

        # Active: while pending, a 0.25s advance fires a poll (re-observes).
        rig.set_delay('-0.025 s')                      # value changes
        rig.advance(ACTIVE)                            # active tick fires
        assert rig.session.watch_pending[0] == -25     # the poll ran and saw it

    def test_stale_chain_is_inert_after_reopen(self, rig):
        # Stop, start a fresh session: a hand-posted WatchTick stamped with the
        # OLD session id does nothing and reschedules nothing.
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')
        old_id = rig.session.session_id

        rig.post(events.PlaybackStopped())
        assert rig.session is None
        assert not rig.watching                        # stop cancelled the chain

        rig.post(events.PlaybackStarted())             # a brand-new session
        assert rig.session.session_id != old_id

        rig.post(events.WatchTick(session_id=old_id))  # stale stamp
        assert rig.offset_table.stored == []
        assert rig.saved == []
        assert not rig.watching                        # nothing rescheduled

    def test_playback_ended_cancels_the_chain(self, rig):
        profile = make_profile()
        rig.begin(profile, baseline_delay='0.000 s')
        assert rig.watching

        rig.post(events.PlaybackEnded())
        assert not rig.watching
