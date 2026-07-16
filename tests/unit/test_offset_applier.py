"""Unit tests for aom.app.offset_applier (OffsetApplier).

Rig pattern shared with the sibling app suites: FakeClock + manually pumped
Dispatcher + real SessionTracker (subscribed first), a scriptable FakeGateway,
and small local settings/offset fakes (the applier's read surface is tiny).
OffsetApplied posts are collected off the bus.

The applied-before-RPC ordering contract also has cross-component pins in
test_session_flow.py; here it is asserted directly at the gateway boundary.

E2: the applier resolves through the sparse-store adapter — hit/fallback
apply, MISS IS A NO-OP (D3: Kodi's delay untouched, one debug line per
distinct consulted chain), and the classic new_install/per-HDR gates are
replaced by the single global pause (D9).
"""

import pytest

from resources.lib.aom.app import events
from resources.lib.aom.app.dispatcher import Dispatcher
from resources.lib.aom.app.offset_applier import OffsetApplier
from resources.lib.aom.app.session import SessionTracker
from resources.lib.aom.domain.profile import StreamProfile
from tests.fakes import FakeClock, FakeGateway, FakeOffsetTable

ALL_KEY = 'dolbyvision|all|truehd'


def make_profile(hdr_type='dolbyvision', audio_format='truehd',
                 video_fps=23.976, player_id=1):
    return StreamProfile(hdr_type=hdr_type, audio_format=audio_format,
                         video_fps=video_fps, player_id=player_id,
                         audio_channels=8)


class FakeSettings:
    """The applier's settings read surface: the global pause only."""

    def __init__(self):
        self.paused = False

    def pause_enabled(self):
        return self.paused


class Rig:
    def __init__(self):
        self.clock = FakeClock()
        self.errors = []
        self.debug = []
        self.warnings = []
        self.dispatcher = Dispatcher(clock=self.clock,
                                     log_error=self.errors.append,
                                     log_debug=self.debug.append)
        self.tracker = SessionTracker(self.dispatcher, clock=self.clock,
                                      log_debug=self.debug.append)
        self.gateway = FakeGateway()
        self.settings = FakeSettings()
        self.offsets = FakeOffsetTable()
        self.applier = OffsetApplier(
            self.dispatcher, self.tracker, self.gateway, self.settings,
            self.offsets, log_debug=self.debug.append,
            log_warning=self.warnings.append)
        self.announced = []
        self.dispatcher.subscribe(events.OffsetApplied, self.announced.append)

    def post(self, event):
        self.dispatcher.post(event)
        self.dispatcher.run_pending()

    @property
    def session(self):
        return self.tracker.current

    def start(self, profile, offset_ms=-125, key=ALL_KEY):
        """Session with a hand-set profile (the detector isn't in the rig)."""
        self.post(events.PlaybackStarted())
        session = self.session
        session.profile = profile
        session.mark_profile_built()        # STARTING -> STABILIZING
        if offset_ms is not None:
            self.offsets.offsets[key] = offset_ms
        return session

    def profile_changed(self):
        self.post(events.ProfileChanged(session_id=self.session.session_id))

    def logged(self, needle):
        return any(needle in line for line in self.debug)


@pytest.fixture
def rig():
    return Rig()


class TestApplyPath:

    def test_applies_on_profile_changed_and_announces_provisional(self, rig):
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)

        rig.profile_changed()

        assert rig.gateway.applied == [(1, -0.125)]
        assert session.applied == (ALL_KEY, -125)
        assert len(rig.announced) == 1
        announced = rig.announced[0]
        assert announced.session_id == session.session_id
        assert announced.profile == profile
        assert announced.ms == -125
        assert announced.provisional is True       # not yet STABLE
        assert rig.logged('session#1')             # describe() snapshot line
        assert rig.logged('hit=exact')             # hit_kind travels to logs

    def test_stable_session_announces_non_provisional(self, rig):
        profile = make_profile()
        session = rig.start(profile)
        session.mark_stable()                       # STABILIZING -> STABLE

        rig.profile_changed()

        assert rig.announced[0].provisional is False

    def test_applied_is_recorded_before_the_rpc(self, rig):
        # The watcher self-echo contract, pinned at the gateway boundary.
        profile = make_profile()
        session = rig.start(profile, offset_ms=-75)
        seen = []

        original = rig.gateway.set_audio_delay

        def spying(player_id, delay_seconds):
            seen.append(session.applied)
            return original(player_id, delay_seconds)

        rig.gateway.set_audio_delay = spying
        rig.profile_changed()

        assert seen == [(ALL_KEY, -75)]

    def test_dedupe_skips_second_apply_for_same_offset(self, rig):
        profile = make_profile()
        rig.start(profile)
        rig.profile_changed()
        rig.post(events.StreamStabilized(session_id=rig.session.session_id))

        assert len(rig.gateway.applied) == 1        # retry edge deduped
        assert len(rig.announced) == 1
        assert rig.logged('skipping duplicate apply')

    def test_changed_offset_reapplies(self, rig):
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)
        rig.profile_changed()

        rig.offsets.offsets[ALL_KEY] = -150   # user re-taught the value
        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert rig.gateway.applied == [(1, -0.125), (1, -0.150)]
        assert session.applied == (ALL_KEY, -150)

    def test_failed_rpc_restores_applied_and_retries_on_stabilization(self, rig):
        profile = make_profile()
        session = rig.start(profile)

        calls = []

        def failing(player_id, delay_seconds):
            calls.append((player_id, delay_seconds))
            return False

        rig.gateway.set_audio_delay = failing
        rig.profile_changed()

        assert session.applied is None              # restored on failure
        assert rig.announced == []                  # no announcement
        assert any('will retry' in m for m in rig.warnings)

        rig.gateway.set_audio_delay = lambda p, s: calls.append((p, s)) or True
        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert len(calls) == 2                      # the retry edge fired
        assert session.applied == (ALL_KEY, -125)
        assert len(rig.announced) == 1

    def test_zero_offset_is_applied(self, rig):
        # A stored 0 means "reset the delay" — a real entry, not a miss.
        profile = make_profile()
        session = rig.start(profile, offset_ms=0)

        rig.profile_changed()

        assert rig.gateway.applied == [(1, 0.0)]
        assert session.applied == (ALL_KEY, 0)

    def test_fallback_hit_applies_the_all_entry(self, rig):
        # per_fps ON with only the all-level taught: the fallback level
        # serves the apply, and hit_kind says so in the logs.
        rig.offsets.per_fps = True
        profile = make_profile(video_fps=60.0)
        session = rig.start(profile, offset_ms=-125, key=ALL_KEY)

        rig.profile_changed()

        assert rig.gateway.applied == [(1, -0.125)]
        assert session.applied == (ALL_KEY, -125)
        assert rig.logged('hit=fallback')


class TestMissIsNoOp:

    def test_miss_applies_nothing_and_logs_once(self, rig):
        # D3: empty store -> no RPC, Kodi's delay untouched, ONE debug line.
        session = rig.start(make_profile(), offset_ms=None)

        rig.profile_changed()
        rig.post(events.StreamStabilized(session_id=session.session_id))
        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert rig.gateway.applied == []
        assert rig.announced == []
        miss_lines = [m for m in rig.debug if 'no stored offset' in m]
        assert len(miss_lines) == 1                 # once per distinct chain
        assert ALL_KEY in miss_lines[0]             # the tried chain is shown

    def test_learned_value_applies_after_a_miss(self, rig):
        # The learn loop's second half: miss now, taught later, applied on
        # the next apply trigger.
        session = rig.start(make_profile(), offset_ms=None)
        rig.profile_changed()
        assert rig.gateway.applied == []

        rig.offsets.offsets[ALL_KEY] = 175          # the user taught it
        rig.post(events.StreamStabilized(session_id=session.session_id))

        assert rig.gateway.applied == [(1, 0.175)]
        assert session.applied == (ALL_KEY, 175)


class TestGating:

    def test_paused_skips(self, rig):
        rig.settings.paused = True
        rig.start(make_profile())
        rig.profile_changed()
        assert rig.gateway.applied == []
        assert rig.logged('paused')

    def test_incomplete_profile_skips(self, rig):
        rig.start(make_profile(audio_format='unknown'))
        rig.profile_changed()
        assert rig.gateway.applied == []
        assert rig.logged('profile incomplete')

    def test_no_profile_skips(self, rig):
        rig.post(events.PlaybackStarted())
        rig.post(events.ProfileChanged(session_id=rig.session.session_id))
        assert rig.gateway.applied == []
        assert rig.logged('No stream profile available')

    def test_invalid_player_id_skips(self, rig):
        rig.start(make_profile(player_id=-1))
        rig.profile_changed()
        assert rig.gateway.applied == []
        assert rig.logged('No valid player ID')

    def test_stale_session_stamp_is_inert(self, rig):
        rig.start(make_profile())
        rig.post(events.ProfileChanged(session_id=999))
        assert rig.gateway.applied == []
        assert rig.announced == []


class TestZeroReset:
    """D3 amendment (E7): miss = no-op until AOM acts, then zero-reset."""

    DELAY_LABEL = 'Player.AudioDelay'

    def _switch_to_unlearned(self, rig, session):
        """Learned profile applied, then the stream becomes an unlearned one."""
        rig.profile_changed()                        # applies ALL_KEY value
        session.profile = make_profile(audio_format='ac3')
        session.miss_announced = None                # fresh episode
        rig.profile_changed()                        # resolves to a miss

    def test_first_miss_of_a_session_touches_nothing(self, rig):
        # P1: no prior AOM action -> the miss leaves Kodi's delay (and any
        # per-file memory the user relies on) completely alone.
        profile = make_profile()
        rig.start(profile, offset_ms=None)           # empty store
        rig.gateway.infolabels[self.DELAY_LABEL] = '0.175 s'

        rig.profile_changed()

        assert rig.gateway.applied == []             # no RPC of any kind

    def test_miss_after_apply_resets_to_baseline_silently(self, rig):
        discarded = []
        rig.dispatcher.subscribe(events.UnsavedOffsetDiscarded,
                                 discarded.append)
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)
        # Kodi echoes our own apply: pure AOM residue.
        rig.gateway.infolabels[self.DELAY_LABEL] = '-0.125 s'

        self._switch_to_unlearned(rig, session)

        assert rig.gateway.applied == [(1, -0.125), (1, 0.0)]
        assert session.applied == (None, 0)
        assert discarded == []                       # silent: our residue
        assert rig.logged('reset delay to 0ms')

    def test_divergent_delay_posts_unsaved_discarded(self, rig):
        # The value in force contains a manual adjustment that never
        # reached the store (remember off, or inside the quiescence
        # window): the reset still happens, and the typed event carries
        # the discarded value for the "Offset not saved" toast.
        discarded = []
        rig.dispatcher.subscribe(events.UnsavedOffsetDiscarded,
                                 discarded.append)
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)
        rig.gateway.infolabels[self.DELAY_LABEL] = '-0.050 s'  # user's hand

        self._switch_to_unlearned(rig, session)

        assert rig.gateway.applied[-1] == (1, 0.0)
        assert len(discarded) == 1
        assert discarded[0].ms == -50
        assert discarded[0].session_id == session.session_id
        assert discarded[0].profile == session.profile

    def test_delay_already_at_baseline_is_left_alone(self, rig):
        profile = make_profile()
        session = rig.start(profile, offset_ms=0)    # stored 0 applies
        rig.gateway.infolabels[self.DELAY_LABEL] = '0.000 s'

        self._switch_to_unlearned(rig, session)

        # The stored-0 apply happened; NO reset RPC followed it.
        assert rig.gateway.applied == [(1, 0.0)]
        assert session.applied == (ALL_KEY, 0)       # apply record intact

    def test_unreadable_delay_resets_silently(self, rig):
        # A parse hiccup can't distinguish residue from a manual value:
        # the doctrine's action (reset) still runs, the toast never does.
        discarded = []
        rig.dispatcher.subscribe(events.UnsavedOffsetDiscarded,
                                 discarded.append)
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)
        rig.gateway.infolabels[self.DELAY_LABEL] = 'garbage'

        self._switch_to_unlearned(rig, session)

        assert rig.gateway.applied[-1] == (1, 0.0)
        assert discarded == []

    def test_failed_reset_rpc_restores_applied_for_retry(self, rig):
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)
        rig.gateway.infolabels[self.DELAY_LABEL] = '-0.125 s'
        rig.profile_changed()                        # apply lands

        calls = []

        def failing(player_id, seconds):
            calls.append((player_id, seconds))
            return False

        rig.gateway.set_audio_delay = failing
        session.profile = make_profile(audio_format='ac3')
        session.miss_announced = None
        rig.profile_changed()

        assert calls == [(1, 0.0)]
        # applied restored so the retry pass re-attempts the reset.
        assert session.applied == (ALL_KEY, -125)
        assert any('reset RPC failed' in line for line in rig.warnings)

    def test_paused_addon_never_resets(self, rig):
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)
        rig.gateway.infolabels[self.DELAY_LABEL] = '-0.125 s'
        rig.profile_changed()

        rig.settings.paused = True
        session.profile = make_profile(audio_format='ac3')
        session.miss_announced = None
        rig.profile_changed()

        assert rig.gateway.applied == [(1, -0.125)]  # only the apply


class TestDeletedReset:
    """D3 second amendment (E7): a marked miss forces the promised 0."""

    DELAY_LABEL = 'Player.AudioDelay'

    def _collect_resets(self, rig):
        fired = []
        rig.dispatcher.subscribe(events.DeletedProfileReset, fired.append)
        return fired

    def test_marked_miss_forces_zero_before_any_aom_action(self, rig):
        # The whole point: the user's delete authorizes the reset, so the
        # P1 "wait until we've acted" gate does NOT apply.
        fired = self._collect_resets(rig)
        profile = make_profile()
        session = rig.start(profile, offset_ms=None)     # empty store
        rig.offsets.resets = {ALL_KEY}                   # deleted in the view
        rig.gateway.infolabels[self.DELAY_LABEL] = '-0.100 s'

        rig.profile_changed()

        assert rig.gateway.applied == [(1, 0.0)]
        assert session.applied == (None, 0)
        assert rig.offsets.consumed == [ALL_KEY]         # one-shot
        assert len(fired) == 1
        assert fired[0].ms == -100                       # the wiped value
        assert fired[0].session_id == session.session_id
        assert rig.logged('reset delay to 0ms for deleted')

    def test_marked_miss_with_delay_already_zero_consumes_silently(self, rig):
        fired = self._collect_resets(rig)
        profile = make_profile()
        rig.start(profile, offset_ms=None)
        rig.offsets.resets = {ALL_KEY}
        rig.gateway.infolabels[self.DELAY_LABEL] = '0.000 s'

        rig.profile_changed()

        assert rig.gateway.applied == []                 # nothing to wipe
        assert rig.offsets.consumed == [ALL_KEY]         # marker still spent
        assert fired == []

    def test_unreadable_delay_still_resets_but_never_toasts(self, rig):
        fired = self._collect_resets(rig)
        profile = make_profile()
        rig.start(profile, offset_ms=None)
        rig.offsets.resets = {ALL_KEY}
        rig.gateway.infolabels[self.DELAY_LABEL] = 'garbage'

        rig.profile_changed()

        assert rig.gateway.applied == [(1, 0.0)]
        assert rig.offsets.consumed == [ALL_KEY]
        assert fired == []                               # never toast a hiccup

    def test_failed_reset_rpc_keeps_the_marker_for_retry(self, rig):
        fired = self._collect_resets(rig)
        profile = make_profile()
        session = rig.start(profile, offset_ms=None)
        rig.offsets.resets = {ALL_KEY}
        rig.gateway.infolabels[self.DELAY_LABEL] = '-0.100 s'
        rig.gateway.set_audio_delay = lambda _pid, _s: False

        rig.profile_changed()

        assert rig.offsets.consumed == []                # marker survives
        assert rig.offsets.resets == {ALL_KEY}
        assert session.applied is None                   # restored
        assert fired == []
        assert any('deleted-profile reset RPC failed' in line
                   for line in rig.warnings)

    def test_hit_consumes_a_stale_marker_silently(self, rig):
        # Deleted exact entry over a KEPT 'all' fallback: the fallback wins
        # (the user kept it) and its apply overwrites any residue — the
        # stale marker is spent without a reset or a toast.
        fired = self._collect_resets(rig)
        rig.offsets.per_fps = True
        exact_key = 'dolbyvision|23|truehd'
        profile = make_profile()
        session = rig.start(profile, offset_ms=-25)      # seeds ALL_KEY
        rig.offsets.resets = {exact_key}

        rig.profile_changed()

        assert rig.gateway.applied == [(1, -0.025)]      # the fallback value
        assert session.applied == (ALL_KEY, -25)
        assert rig.offsets.consumed == [exact_key]
        assert fired == []
        assert len(rig.announced) == 1                   # normal apply toast
