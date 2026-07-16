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
from resources.lib.aom.app.adjustment_watcher import AdjustmentWatcher
from resources.lib.aom.app.dispatcher import Dispatcher
from resources.lib.aom.app.offset_applier import OffsetApplier
from resources.lib.aom.app.session import SessionTracker
from resources.lib.aom.domain.profile import StreamProfile
from tests.fakes import FakeClock, FakeGateway, FakeOffsetTable

ALL_KEY = 'dolbyvision|all|truehd'
# make_profile()'s default 23.976 fps, integer-truncated by the key schema.
EXACT_KEY = 'dolbyvision|23|truehd'
# The label the applier's reset paths read — bound to the production
# constant so a renamed infolabel cannot leave these tests green-but-wrong.
DELAY_LABEL = AdjustmentWatcher.INFOLABEL_AUDIO_DELAY


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

    def settings_changed(self):
        self.post(events.SettingsChanged())

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
        rig.gateway.infolabels[DELAY_LABEL] = '0.175 s'

        rig.profile_changed()

        assert rig.gateway.applied == []             # no RPC of any kind

    def test_miss_after_apply_resets_to_baseline_silently(self, rig):
        discarded = []
        rig.dispatcher.subscribe(events.UnsavedOffsetDiscarded,
                                 discarded.append)
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)
        # Kodi echoes our own apply: pure AOM residue.
        rig.gateway.infolabels[DELAY_LABEL] = '-0.125 s'

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
        rig.gateway.infolabels[DELAY_LABEL] = '-0.050 s'  # user's hand

        self._switch_to_unlearned(rig, session)

        assert rig.gateway.applied[-1] == (1, 0.0)
        assert len(discarded) == 1
        assert discarded[0].ms == -50
        assert discarded[0].session_id == session.session_id
        assert discarded[0].profile == session.profile

    def test_delay_already_at_baseline_is_left_alone(self, rig):
        profile = make_profile()
        session = rig.start(profile, offset_ms=0)    # stored 0 applies
        rig.gateway.infolabels[DELAY_LABEL] = '0.000 s'

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
        rig.gateway.infolabels[DELAY_LABEL] = 'garbage'

        self._switch_to_unlearned(rig, session)

        assert rig.gateway.applied[-1] == (1, 0.0)
        assert discarded == []

    def test_failed_reset_rpc_restores_applied_for_retry(self, rig):
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)
        rig.gateway.infolabels[DELAY_LABEL] = '-0.125 s'
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

    def test_successful_reset_posts_delay_reset(self, rig):
        # Every delay the applier sets announces itself: the silent reset
        # posts DelayReset (the watcher's structural supersede) — but only
        # when an RPC actually landed; the already-0 branch posts nothing.
        resets = []
        rig.dispatcher.subscribe(events.DelayReset, resets.append)
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)
        rig.gateway.infolabels[DELAY_LABEL] = '-0.125 s'

        self._switch_to_unlearned(rig, session)

        assert [e.session_id for e in resets] == [session.session_id]

    def test_no_delay_reset_event_without_an_rpc(self, rig):
        resets = []
        rig.dispatcher.subscribe(events.DelayReset, resets.append)
        profile = make_profile()
        session = rig.start(profile, offset_ms=0)     # stored 0 applies
        rig.gateway.infolabels[DELAY_LABEL] = '0.000 s'

        self._switch_to_unlearned(rig, session)       # already at baseline

        assert resets == []

    def test_paused_addon_never_resets(self, rig):
        profile = make_profile()
        session = rig.start(profile, offset_ms=-125)
        rig.gateway.infolabels[DELAY_LABEL] = '-0.125 s'
        rig.profile_changed()

        rig.settings.paused = True
        session.profile = make_profile(audio_format='ac3')
        session.miss_announced = None
        rig.profile_changed()

        assert rig.gateway.applied == [(1, -0.125)]  # only the apply


class TestSettingsChangedReapply:
    """Immediate-effect edge (E7): a settings save re-runs the decision.

    Every decision input is already read at decision instant (the per_fps
    toggle inside resolve, the pause gate in the policy), so the trigger
    itself is most of the feature. The one trigger-specific divergence
    (E7 review): a save changes no profile, so a foreign delay — the
    user's hand — survives the miss path's baseline reset; only our own
    orphaned residue is reset by a save.
    """

    def test_no_session_is_a_no_op(self, rig):
        rig.settings_changed()

        assert rig.gateway.applied == []
        assert rig.errors == []

    def test_no_profile_yet_skips_quietly(self, rig):
        # Settings saved during discovery (profile not adopted yet): the
        # normal no-profile gate answers; nothing to reconcile.
        rig.post(events.PlaybackStarted())

        rig.settings_changed()

        assert rig.gateway.applied == []
        assert rig.logged('No stream profile available')

    def test_unaffecting_save_dedupes_to_a_no_op(self, rig):
        # Kodi may fire onSettingsChanged several times per dialog save;
        # every firing whose resolution is unchanged lands in the dedupe.
        session = rig.start(make_profile(), offset_ms=-125)
        rig.profile_changed()

        for _ in range(3):
            rig.settings_changed()

        assert rig.gateway.applied == [(1, -0.125)]     # no second RPC
        assert session.applied == (ALL_KEY, -125)
        assert len(rig.announced) == 1
        assert rig.logged('skipping duplicate apply')

    def test_per_fps_flip_applies_the_exact_entry_immediately(self, rig):
        # The headline scenario: all-level value in force, user flips the
        # per-fps toggle mid-playback, the taught exact entry lands NOW.
        session = rig.start(make_profile(), offset_ms=75)   # all-level taught
        session.mark_stable()
        rig.profile_changed()
        assert rig.gateway.applied == [(1, 0.075)]

        rig.offsets.offsets[EXACT_KEY] = -25
        rig.offsets.per_fps = True                          # the settings flip
        rig.settings_changed()

        assert rig.gateway.applied == [(1, 0.075), (1, -0.025)]
        assert session.applied == (EXACT_KEY, -25)
        assert rig.announced[-1].provisional is False       # STABLE session
        assert rig.announced[-1].ms == -25

    def test_per_fps_off_orphaning_the_exact_entry_zero_resets(self, rig):
        # Flip OFF with only the exact level taught: the profile is now
        # unlearned at the all level, AOM has acted, and the delay in force
        # is OUR OWN residue (echoes the apply) — the save itself orphaned
        # it, so the standing zero-reset doctrine runs, silently.
        discarded = []
        rig.dispatcher.subscribe(events.UnsavedOffsetDiscarded,
                                 discarded.append)
        rig.offsets.per_fps = True
        session = rig.start(make_profile(), offset_ms=-25, key=EXACT_KEY)
        rig.profile_changed()
        assert session.applied == (EXACT_KEY, -25)
        rig.gateway.infolabels[DELAY_LABEL] = '-0.025 s'    # our residue

        rig.offsets.per_fps = False                         # the settings flip
        rig.settings_changed()

        assert rig.gateway.applied == [(1, -0.025), (1, 0.0)]
        assert session.applied == (None, 0)
        assert discarded == []                              # silent reset

    def test_settings_save_preserves_foreign_delay_on_miss(self, rig):
        # E7 review finding (the wipe): a save changes no profile, so a
        # delay that DIVERGED from our last apply is the user's hand (an
        # in-flight dial, or a deliberate session value with remember off)
        # and still targets the stream in force. A stream change would
        # reset it with the not-saved toast; a settings save must not.
        discarded = []
        rig.dispatcher.subscribe(events.UnsavedOffsetDiscarded,
                                 discarded.append)
        rig.offsets.per_fps = True
        session = rig.start(make_profile(), offset_ms=-25, key=EXACT_KEY)
        rig.profile_changed()
        rig.gateway.infolabels[DELAY_LABEL] = '-0.050 s'    # the user's dial

        rig.offsets.per_fps = False                         # orphaning flip
        rig.settings_changed()

        assert rig.gateway.applied == [(1, -0.025)]         # no reset RPC
        assert session.applied == (EXACT_KEY, -25)          # bookkeeping intact
        assert discarded == []                              # and no toast
        assert rig.logged('not ours to reset')

    def test_settings_save_leaves_unreadable_delay_alone(self, rig):
        # On the settings path an unreadable delay is a hiccup over a
        # profile that did not change — never act on it (a stream change
        # keeps its reset-silently doctrine, pinned in TestZeroReset).
        rig.offsets.per_fps = True
        session = rig.start(make_profile(), offset_ms=-25, key=EXACT_KEY)
        rig.profile_changed()
        rig.gateway.infolabels[DELAY_LABEL] = 'garbage'

        rig.offsets.per_fps = False
        rig.settings_changed()

        assert rig.gateway.applied == [(1, -0.025)]         # no reset RPC
        assert session.applied == (EXACT_KEY, -25)

    def test_miss_resolution_drops_a_held_provisional_toast(self, rig):
        # E7 review finding (the stale toast): a held provisional "applied
        # X" cannot survive a miss resolution — without this, a settings-
        # save reset during stabilization leaves the hold intact and the
        # notifier releases a toast for a value no longer in force (the
        # profile identity check cannot catch it: the profile is unchanged).
        rig.offsets.per_fps = True
        session = rig.start(make_profile(), offset_ms=-25, key=EXACT_KEY)
        rig.profile_changed()                               # provisional apply
        session.pending_notification = (session.profile, -25)  # notifier's hold
        rig.gateway.infolabels[DELAY_LABEL] = '-0.025 s'

        rig.offsets.per_fps = False                         # orphaning flip
        rig.settings_changed()                              # miss -> reset

        assert rig.gateway.applied == [(1, -0.025), (1, 0.0)]
        assert session.pending_notification is None         # hold dropped

    def test_miss_with_no_prior_action_stays_untouched(self, rig):
        # P1 holds under the new trigger: unlearned profile, no AOM action
        # this session — a settings save must not disturb Kodi's delay.
        rig.start(make_profile(), offset_ms=None)           # empty store
        rig.profile_changed()
        rig.gateway.infolabels[DELAY_LABEL] = '0.175 s'     # user/Kodi value

        rig.settings_changed()

        assert rig.gateway.applied == []

    def test_unpausing_applies_immediately(self, rig):
        rig.settings.paused = True
        session = rig.start(make_profile(), offset_ms=-125)
        rig.profile_changed()
        assert rig.gateway.applied == []                    # gated off

        rig.settings.paused = False                         # user unpauses
        rig.settings_changed()

        assert rig.gateway.applied == [(1, -0.125)]
        assert session.applied == (ALL_KEY, -125)

    def test_pausing_gates_but_never_reverts(self, rig):
        session = rig.start(make_profile(), offset_ms=-125)
        rig.profile_changed()

        rig.settings.paused = True                          # user pauses
        rig.settings_changed()

        assert rig.gateway.applied == [(1, -0.125)]         # left in force
        assert session.applied == (ALL_KEY, -125)
        assert rig.logged('paused')

    def test_deleted_live_profile_resets_on_settings_save(self, rig):
        # A settings save is a resolve moment, so a pending deletion marker
        # for the LIVE profile acts then — not only at the next stream
        # event. The marked path keeps forcing 0 even where the unmarked
        # path now preserves: the user's delete IS the authorization.
        session = rig.start(make_profile(), offset_ms=-125)
        rig.profile_changed()
        rig.gateway.infolabels[DELAY_LABEL] = '-0.125 s'

        del rig.offsets.offsets[ALL_KEY]                    # manage-view delete
        rig.offsets.resets = {ALL_KEY}
        rig.settings_changed()

        assert rig.gateway.applied == [(1, -0.125), (1, 0.0)]
        assert session.applied == (None, 0)
        assert rig.offsets.consumed == [ALL_KEY]            # marker spent
        assert rig.logged('reset delay to 0ms for deleted')


class TestStoreMutatedReapply:
    """Management-view edge (E7 user call, 2026-07-16): a store-changing
    mutation is a resolve moment — deleting the PLAYING profile's offset
    acts immediately. Same profile_unchanged semantics as a settings save
    (the mutation changes no profile either)."""

    def store_mutated(self, rig, op='delete', key=None):
        rig.post(events.StoreMutated(op=op, key=key))

    def test_deleting_the_live_profiles_entry_resets_immediately(self, rig):
        session = rig.start(make_profile(), offset_ms=-125)
        rig.profile_changed()
        rig.gateway.infolabels[DELAY_LABEL] = '-0.125 s'

        del rig.offsets.offsets[ALL_KEY]        # the view's delete...
        rig.offsets.resets = {ALL_KEY}          # ...leaves its marker
        self.store_mutated(rig, key=ALL_KEY)

        assert rig.gateway.applied == [(1, -0.125), (1, 0.0)]
        assert session.applied == (None, 0)
        assert rig.offsets.consumed == [ALL_KEY]
        assert rig.logged('reset delay to 0ms for deleted')

    def test_unrelated_mutation_dedupes_to_a_no_op(self, rig):
        session = rig.start(make_profile(), offset_ms=-125)
        rig.profile_changed()

        self.store_mutated(rig, key='hdr10|all|eac3')

        assert rig.gateway.applied == [(1, -0.125)]
        assert session.applied == (ALL_KEY, -125)

    def test_no_session_is_a_no_op(self, rig):
        self.store_mutated(rig)

        assert rig.gateway.applied == []
        assert rig.errors == []

    def test_paused_addon_holds_the_marker(self, rig):
        # The standing gates hold on this trigger too: a paused addon does
        # nothing, and the marker stays pending for a later unpaused
        # resolve instead of being consumed blind.
        session = rig.start(make_profile(), offset_ms=-125)
        rig.profile_changed()
        rig.settings.paused = True
        del rig.offsets.offsets[ALL_KEY]
        rig.offsets.resets = {ALL_KEY}

        self.store_mutated(rig, key=ALL_KEY)

        assert rig.gateway.applied == [(1, -0.125)]      # untouched
        assert rig.offsets.consumed == []
        assert rig.offsets.resets == {ALL_KEY}           # marker survives
        assert session.applied == (ALL_KEY, -125)

    def test_mutation_preserves_foreign_delay_on_unmarked_miss(self, rig):
        # Deleting an UNRELATED entry while the live profile is an
        # unlearned (unmarked) miss with the user's dial in force: the
        # profile_unchanged rule holds here too — someone else's delete
        # never wipes the dial.
        discarded = []
        rig.dispatcher.subscribe(events.UnsavedOffsetDiscarded,
                                 discarded.append)
        session = rig.start(make_profile(), offset_ms=-125)
        rig.profile_changed()                            # applies -125
        rig.gateway.infolabels[DELAY_LABEL] = '-0.125 s'
        session.profile = make_profile(audio_format='ac3')  # unlearned stream
        session.miss_announced = None
        rig.profile_changed()                            # stream reset to 0
        assert session.applied == (None, 0)
        rig.gateway.infolabels[DELAY_LABEL] = '-0.050 s'  # user dials on it

        self.store_mutated(rig, key='hdr10|all|eac3')    # unrelated delete

        assert rig.gateway.applied == [(1, -0.125), (1, 0.0)]  # no 3rd RPC
        assert discarded == []
        assert rig.logged('not ours to reset')


class TestDeletedReset:
    """D3 second amendment (E7): a marked miss forces the promised 0.

    SILENT by design (user call, same day): 0 is the implicit, expected
    outcome of the deletion, so NO event and NO toast exist for it — the
    ``announced`` collector doubles as the no-event pin here.
    """

    def test_marked_miss_forces_zero_before_any_aom_action(self, rig):
        # The whole point: the user's delete authorizes the reset, so the
        # P1 "wait until we've acted" gate does NOT apply.
        profile = make_profile()
        session = rig.start(profile, offset_ms=None)     # empty store
        rig.offsets.resets = {ALL_KEY}                   # deleted in the view
        rig.gateway.infolabels[DELAY_LABEL] = '-0.100 s'

        rig.profile_changed()

        assert rig.gateway.applied == [(1, 0.0)]
        assert session.applied == (None, 0)
        assert rig.offsets.consumed == [ALL_KEY]         # one-shot
        assert rig.announced == []                       # silent: no toast
        assert rig.logged('reset delay to 0ms for deleted')

    def test_marked_miss_with_delay_already_zero_consumes_silently(self, rig):
        profile = make_profile()
        rig.start(profile, offset_ms=None)
        rig.offsets.resets = {ALL_KEY}
        rig.gateway.infolabels[DELAY_LABEL] = '0.000 s'

        rig.profile_changed()

        assert rig.gateway.applied == []                 # nothing to wipe
        assert rig.offsets.consumed == [ALL_KEY]         # marker still spent

    def test_unreadable_delay_still_resets(self, rig):
        profile = make_profile()
        rig.start(profile, offset_ms=None)
        rig.offsets.resets = {ALL_KEY}
        rig.gateway.infolabels[DELAY_LABEL] = 'garbage'

        rig.profile_changed()

        assert rig.gateway.applied == [(1, 0.0)]
        assert rig.offsets.consumed == [ALL_KEY]

    def test_failed_reset_rpc_keeps_the_marker_for_retry(self, rig):
        profile = make_profile()
        session = rig.start(profile, offset_ms=None)
        rig.offsets.resets = {ALL_KEY}
        rig.gateway.infolabels[DELAY_LABEL] = '-0.100 s'
        rig.gateway.set_audio_delay = lambda _pid, _s: False

        rig.profile_changed()

        assert rig.offsets.consumed == []                # marker survives
        assert rig.offsets.resets == {ALL_KEY}
        assert session.applied is None                   # restored
        assert any('deleted-profile reset RPC failed' in line
                   for line in rig.warnings)

    def test_forced_reset_posts_delay_reset(self, rig):
        # The marker-forced 0 is an automatic delay change like any other:
        # DelayReset fires so the watcher drops an in-flight observation
        # (otherwise a lagging label could re-store the deleted value).
        resets = []
        rig.dispatcher.subscribe(events.DelayReset, resets.append)
        profile = make_profile()
        session = rig.start(profile, offset_ms=None)
        rig.offsets.resets = {ALL_KEY}
        rig.gateway.infolabels[DELAY_LABEL] = '-0.100 s'

        rig.profile_changed()

        assert [e.session_id for e in resets] == [session.session_id]

    def test_hit_consumes_a_stale_marker_silently(self, rig):
        # Deleted exact entry over a KEPT 'all' fallback: the fallback wins
        # (the user kept it) and its apply overwrites any residue — the
        # stale marker is spent without a reset.
        rig.offsets.per_fps = True
        profile = make_profile()
        session = rig.start(profile, offset_ms=-25)      # seeds ALL_KEY
        rig.offsets.resets = {EXACT_KEY}

        rig.profile_changed()

        assert rig.gateway.applied == [(1, -0.025)]      # the fallback value
        assert session.applied == (ALL_KEY, -25)
        assert rig.offsets.consumed == [EXACT_KEY]
        assert len(rig.announced) == 1                   # normal apply toast
