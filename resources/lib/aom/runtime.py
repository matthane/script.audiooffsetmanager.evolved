"""Composition root for the service process.

Builds the full typed graph — the dispatcher, the Kodi adapters (gateway,
settings, gui, log), and the app components — with explicit, REQUIRED
constructor dependencies: no fallback construction anywhere, exactly one
instance of each adapter for the whole process. Blocks on the monitor until
Kodi aborts, then stops the dispatcher.

Every component subscribes during construction, BEFORE the dispatcher thread
starts, so events the bridges queue during construction are dispatched to a
complete graph (matters when the service (re)starts while playback is
already active).

Subscription order is load-bearing (dispatch follows it, per event type):

1. tracker — the session exists (or is torn down) before any other handler
   of the same lifecycle event runs;
2. detector — owns ``session.profile`` and the stream-state machine (its
   ``StreamProbed`` platform facts are log-only now; the PlatformRecorder
   dissolved with the stored capability flags — P3);
3. applier — on ProfileChanged/StreamStabilized the offset is applied (and
   ``session.applied`` recorded) before anything downstream reads it;
4. notifier — its StreamStabilized release runs after the applier's retry
   pass for the same stabilization;
5. seek scheduler — seeks for a stabilization are planned only after the
   offset work for it is done;
6. adjustment watcher — its ProfileChanged eligibility pass runs last, so
   ``session.applied`` is already current when the first watch tick of a
   profile episode is scheduled.
"""

import xbmcvfs

from resources.lib.aom.app import events
from resources.lib.aom.app.adjustment_watcher import AdjustmentWatcher
from resources.lib.aom.app.dispatcher import Dispatcher
from resources.lib.aom.app.notifier import Notifier
from resources.lib.aom.app.offset_applier import OffsetApplier
from resources.lib.aom.app.seek_scheduler import (ExternalSeekCoordinator,
                                                  SeekScheduler)
from resources.lib.aom.app.session import SessionTracker
from resources.lib.aom.app.store_mutations import (ACK_MESSAGE,
                                                   StoreMutationHandler)
from resources.lib.aom.app.stream_detector import StreamDetector
from resources.lib.aom.kodi.gateway import KodiGateway
from resources.lib.aom.kodi.gui import Gui
from resources.lib.aom.kodi.log import KodiLogger
from resources.lib.aom.kodi.monitor_bridge import MonitorBridge
from resources.lib.aom.kodi.player_bridge import PlayerBridge
from resources.lib.aom.kodi.settings import ADDON_ID, STORE_PATH, Settings
from resources.lib.aom.store.offset_store import OffsetStore
from resources.lib.aom.store.table import OffsetTable


class ServiceRuntime:
    def __init__(self):
        # Adapters first: one instance each, injected everywhere.
        self.logger = KodiLogger()
        self.settings = Settings(log=self.logger)
        self.logger.debug_escalation = self.settings.debug_logging_enabled()
        self.gateway = KodiGateway(log=self.logger)
        self.gui = Gui(log=self.logger)

        # The sparse offset store: loaded ONCE at service start, owned by
        # the dispatcher thread thereafter (single-writer doctrine). A
        # corrupt file was quarantined to .bad inside load(); the typed
        # StoreCorrupted event is posted AFTER the graph is built (below)
        # and the Notifier owns the user-facing notice.
        self.store = OffsetStore(
            xbmcvfs.translatePath(STORE_PATH),
            log_debug=self.logger.debug, log_warning=self.logger.warning)
        self.store.load()
        self.offsets = OffsetTable(self.store, self.settings)

        self.dispatcher = Dispatcher(
            log_debug=self.logger.debug,
            log_error=self.logger.error,
            log_runtimes=self.logger.debug_escalation)

        # App components, in the load-bearing subscription order (docstring).
        self.session_tracker = SessionTracker(
            self.dispatcher, log_debug=self.logger.debug)
        self.detector = StreamDetector(
            self.dispatcher, self.session_tracker, self.gateway,
            self.settings, log_debug=self.logger.debug,
            log_warning=self.logger.warning)
        self.offset_applier = OffsetApplier(
            self.dispatcher, self.session_tracker, self.gateway,
            self.settings, self.offsets, log_debug=self.logger.debug,
            log_warning=self.logger.warning)
        self.notifier = Notifier(
            self.dispatcher, self.session_tracker, self.settings, self.gui,
            log_debug=self.logger.debug)
        self.seek_coordinator = ExternalSeekCoordinator(
            self.gateway, log_debug=self.logger.debug)
        self.seek_scheduler = SeekScheduler(
            self.dispatcher, self.session_tracker, self.settings,
            self.seek_coordinator, log_debug=self.logger.debug,
            log_warning=self.logger.warning)
        self.adjustment_watcher = AdjustmentWatcher(
            self.dispatcher, self.session_tracker, self.gateway,
            self.settings, self.offsets, log_debug=self.logger.debug,
            log_warning=self.logger.warning)
        # The cross-process mutation channel's executor (D5): requests
        # arrive via the monitor bridge as typed events, mutate the store
        # on this dispatcher (single-writer doctrine), and ack back over
        # NotifyAll so the script process can tell "done" from "no service".
        self.store_mutations = StoreMutationHandler(
            self.dispatcher, self.session_tracker, self.store,
            lambda payload: self.gateway.notify_all(
                ADDON_ID, ACK_MESSAGE, payload),
            log_debug=self.logger.debug, log_warning=self.logger.warning)

        self.player_bridge = PlayerBridge(self.dispatcher)
        self.monitor = MonitorBridge(self.dispatcher)
        self.dispatcher.subscribe(events.SettingsChanged,
                                  self._on_settings_changed)

        # Surface the one-shot corruption flag through the graph: posted
        # here (queued until the dispatcher starts) so the Notifier — the
        # toast owner — raises the notice, not this composition root.
        if self.store.pop_corruption():
            self.dispatcher.post(events.StoreCorrupted())

    def _on_settings_changed(self, _event):
        """Refresh the cached debug flags; never write settings from here."""
        debug = self.settings.debug_logging_enabled()
        self.logger.debug_escalation = debug
        self.dispatcher.log_runtimes = debug

    def run(self):
        self.dispatcher.start()
        self.logger.debug("AOM_Runtime: service started")

        self.monitor.waitForAbort()

        self.logger.debug("AOM_Runtime: abort requested; shutting down")
        # Joining the dispatcher thread is the whole shutdown: every
        # subscription lives on the dispatcher, and posts arriving after
        # stop are dropped by design.
        self.dispatcher.stop()
