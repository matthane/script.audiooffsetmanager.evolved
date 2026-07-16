"""Shared test fakes for the Audio Offset Manager suite.

``FakeClock`` (Phase 2) is the deterministic clock every phase reuses: the
dispatcher — and every component that measures intervals — takes an injected
``clock`` callable that defaults to ``time.monotonic``. Tests pass a
``FakeClock`` instead so time only moves when the test says so, and
timer-driven behaviour is driven with ``Dispatcher.run_pending()`` rather
than real sleeps.

``FakeGateway`` (Phase 4) is the scriptable stand-in for
``aom.kodi.gateway.KodiGateway``: tests mutate its attributes between pumps
to script what the "platform" reports, mirroring how the real single-shot
gateway reads live Kodi state on every call.

``FakeFacade`` (Phase 5) is the shared settings-facade double covering the
methods app components read (detector: fps_override_enabled; scheduler:
seek_back_config) — one fake, so the facade contract cannot drift between
suites.

``FakeGui`` (Phase 7) is the stand-in for ``aom.kodi.gui.Gui``: it records
toasts and returns a deterministic ``localized()`` marker so the Notifier's
message assembly is asserted without a real string table.

Keep this module tiny and dependency-free (no Kodi imports, no pytest) so
every test tier can share it.
"""


class FakeClock:
    """A deterministic stand-in for ``time.monotonic``.

    Instances are callable and return the current fake time in seconds as a
    float, exactly like ``time.monotonic()`` — so ``Dispatcher(clock=clock)``
    accepts one directly. Time never advances on its own; call
    :meth:`advance` to move it forward.

    The value is monotonic non-decreasing: advancing by a negative amount is
    rejected, preserving the one guarantee real interval math relies on.

    Example::

        clock = FakeClock()
        d = Dispatcher(clock=clock, log_error=errors.append)
        d.schedule(1.0, Tick())
        d.run_pending()      # nothing due yet
        clock.advance(1.0)
        d.run_pending()      # Tick fires
    """

    __slots__ = ("_now",)

    def __init__(self, start=0.0):
        self._now = float(start)

    def __call__(self):
        return self._now

    def advance(self, seconds):
        """Move the clock forward by ``seconds`` (must be >= 0); return the new time."""
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        self._now += float(seconds)
        return self._now


class FakeGateway:
    """Scriptable stand-in for ``aom.kodi.gateway.KodiGateway``.

    Mirrors the real gateway's single-shot semantics: every read reflects the
    CURRENT attribute values, so tests script a stream by mutating
    ``player_id`` / ``codec`` / ``channels`` / ``infolabels`` between pumps
    (exactly how the real gateway sees live Kodi state change under it).
    Write-side calls are recorded for assertions and report success.
    """

    def __init__(self, player_id=1, codec='truehd', channels=8,
                 infolabels=None):
        self.player_id = player_id
        self.codec = codec
        self.channels = channels
        self.infolabels = dict(infolabels or {})
        self.settings_dialog = False   # scripted addon-settings-dialog state
        self.applied = []            # (player_id, delay_seconds)
        self.seeks = []              # (seconds, player_id)
        self.window_properties = {}

    # -- reads ------------------------------------------------------------------

    def active_player_id(self):
        return self.player_id

    def audio_info(self, player_id):
        return self.codec, self.channels

    def infolabel(self, label):
        return self.infolabels.get(label, '')

    def settings_dialog_open(self):
        return self.settings_dialog

    def window_property(self, name):
        return self.window_properties.get(name, '')

    # -- writes -----------------------------------------------------------------

    def set_audio_delay(self, player_id, delay_seconds):
        self.applied.append((player_id, delay_seconds))
        return True

    def seek_back(self, seconds, player_id=None):
        self.seeks.append((seconds, player_id))
        return True

    def set_window_property(self, name, value):
        self.window_properties[name] = value

    def clear_window_property(self, name):
        self.window_properties.pop(name, None)


class FakeFacade:
    """Scriptable settings double covering the app components' read surface.

    ``per_fps`` drives the detector's identity granularity and the offset
    table's key composition; ``seek_configs`` maps a seek reason to its
    (enabled, seconds) pair, defaulting every reason to (True, 4);
    ``remember_adjustments`` / ``paused`` gate the adjustment watcher and
    (paused) the applier; ``notify_apply`` / ``notify_learn`` /
    ``notification_ms`` cover the Notifier's per-kind toast gates and
    duration (D10 defaults: both gates ON). Offset reads/writes live on
    ``FakeOffsetTable`` (matching the real split:
    ``aom.kodi.settings.Settings`` + ``OffsetTable``).
    """

    def __init__(self, per_fps=False):
        self.per_fps = per_fps
        self.seek_configs = {}
        self.remember_adjustments = True
        self.paused = False
        self.notify_apply = True
        self.notify_learn = True
        self.notification_ms = 5000

    def per_fps_offsets_enabled(self):
        return self.per_fps

    def pause_enabled(self):
        return self.paused

    def remember_adjustments_enabled(self):
        return self.remember_adjustments

    def seek_back_config(self, reason):
        return self.seek_configs.get(reason, (True, 4))

    def notify_apply_enabled(self):
        return self.notify_apply

    def notify_learn_enabled(self):
        return self.notify_learn

    def notification_duration_ms(self):
        return self.notification_ms


class FakeOffsetTable:
    """Scriptable stand-in for the store-backed ``OffsetTable`` adapter.

    Backed by a plain dict (key -> ms). ``resolve``/``write_key`` reuse the
    REAL pure functions from ``aom.store.resolve`` so the fake cannot drift
    from the decision table; only persistence is faked. Pass the rig's
    ``FakeFacade`` as ``facade`` so the per-fps toggle has ONE source of
    truth shared with every other consumer (a fake-only split between
    detector identity and table keys cannot exist in the real runtime);
    standalone uses may set ``per_fps`` directly instead.
    """

    def __init__(self, per_fps=False, facade=None):
        self.offsets = {}            # key -> ms
        self.stored = []             # (key, ms), in store order
        self.store_ok = True
        self.read_only = False
        self._facade = facade
        self._per_fps = per_fps

    @property
    def per_fps(self):
        if self._facade is not None:
            return self._facade.per_fps
        return self._per_fps

    @per_fps.setter
    def per_fps(self, value):
        if self._facade is not None:
            self._facade.per_fps = value
        else:
            self._per_fps = value

    # dict-shaped store protocol for resolve.resolve
    def get(self, key):
        if key in self.offsets:
            return {'delay_ms': self.offsets[key]}
        return None

    def resolve(self, profile):
        from resources.lib.aom.store import resolve as store_resolve
        return store_resolve.resolve(
            self, profile.hdr_type, profile.video_fps, profile.audio_format,
            per_fps=self.per_fps)

    def write_key(self, profile):
        from resources.lib.aom.store import resolve as store_resolve
        try:
            return store_resolve.write_key(
                profile.hdr_type, profile.video_fps, profile.audio_format,
                per_fps=self.per_fps)
        except ValueError:
            return None

    def get_at(self, key):
        return self.get(key)

    def stored_ms_at(self, key):
        return self.offsets.get(key)

    def store(self, profile, ms):
        if not self.store_ok:
            return None
        key = self.write_key(profile)
        if key is None:
            return None
        self.stored.append((key, ms))
        self.offsets[key] = ms
        return key


class FakeGui:
    """Records toasts/dialogs; localized() returns a deterministic marker.

    The dialog surfaces mirror the real ``Gui`` (D6 plain dialogs) and are
    scripted by queueing answers: ``select_answers`` (int per call; -1 =
    cancel, and an exhausted queue answers -1 so a view loop always
    terminates) and ``yesno_answers`` (bool per call; exhausted -> False,
    matching the real fake-consent-never rule). Every shown dialog is
    recorded for assertions.
    """

    def __init__(self):
        self.notifications = []          # (message, duration_ms)
        self.titles = []                 # title per notification (lockstep)
        self.localized_strings = {}      # optional overrides: id -> str
        self.select_answers = []         # scripted select() replies (FIFO)
        self.yesno_answers = []          # scripted yesno() replies (FIFO)
        self.selects = []                # (heading, options) per select()
        self.yesnos = []                 # (heading, message) per yesno()
        self.oks = []                    # (heading, message) per ok()

    def localized(self, string_id):
        return self.localized_strings.get(string_id, f"#{string_id}")

    def notification(self, message, duration_ms, title=None, icon=None):
        # Recorded toasts keep the 2-tuple shape the suites assert on; the
        # title (None = the real Gui's addon-name default) is recorded in
        # lockstep for the suites that pin the heading.
        self.notifications.append((message, duration_ms))
        self.titles.append(title)

    def select(self, heading, options):
        self.selects.append((heading, list(options)))
        if self.select_answers:
            return self.select_answers.pop(0)
        return -1

    def yesno(self, heading, message):
        self.yesnos.append((heading, message))
        if self.yesno_answers:
            return self.yesno_answers.pop(0)
        return False

    def ok(self, heading, message):
        self.oks.append((heading, message))
        return True
