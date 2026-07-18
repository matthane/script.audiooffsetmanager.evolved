"""Kodi settings adapter: typed reads/writes plus intent-level accessors.

Replaces the legacy ``SettingsManager`` singleton + ``SettingsFacade`` pair
(Phase 7 deletes them) with a plain, injected class. There is no singleton
here: the runtime constructs exactly one ``Settings`` and injects it into every
component that needs settings (required constructor deps are the house rule,
same as ``KodiGateway``).

Why one instance is enough — and not a correctness barrier: Kodi's
``xbmcaddon.Addon(ADDON_ID).getSettings()`` returns a LIVE proxy onto the
in-process settings store, not a frozen snapshot (see the repo CLAUDE.md
settings doctrine). Every read sees current values and every write is visible
everywhere at once, so the store cannot drift out of sync with itself. Holding
one proxy per process is a tidiness convenience, not the thing that makes
reads/writes consistent.

LIFETIME RULE (field-verified on Kodi 21.2/Windows, 2026-07-15): the proxy is
live ONLY while the ``xbmcaddon.Addon`` it came from stays alive. A
``Settings`` object whose parent ``Addon`` was a garbage-collected temporary
degrades into a detached copy — writes report success but never persist to
the real store or ``settings.xml``, and outside changes (settings dialog
edits) never arrive. ``__init__`` therefore keeps the ``Addon`` on ``self``;
never rewrite it as ``xbmcaddon.Addon(...).getSettings()``.

Faithful port of the legacy semantics, with two deliberate upgrades noted as
Phase 7 work: the bare ``except:`` clauses become ``except Exception`` here, and
the ``AOMe_SettingsManager`` log prefix becomes ``AOMe_Settings``. The
``store_*_if_changed`` helpers keep the read-before-write skip for BEHAVIOR
settings (the only kind that lives here now — offsets moved to the sparse
store, P4): no write means nothing for a dialog save-on-close to fight
over. Their next runtime caller is the E4 coexistence once-flag.

This layer may import ``xbmc*``/``xbmcaddon`` and ``resources.lib.aom.*`` only;
importing any legacy ``resources.lib.<module>`` fails
``tests/contract/test_architecture.py``.
"""

import xbmc
import xbmcaddon
import xbmcvfs

from resources.lib.aom.app.store_mutations import IMPORT_SUFFIX

ADDON_ID = 'script.audiooffsetmanager.evolved'

# The sparse offset store's on-disk home. Lives beside ADDON_ID (addon
# identity constants) because BOTH processes need it: the service runtime
# builds the OffsetStore on it, the script router points the management
# view's read-only reader at it.
STORE_PATH = f'special://profile/addon_data/{ADDON_ID}/offsets.json'


def import_staging_path():
    """The import channel's staged-backup path, translated and ready.

    Derived HERE, once, because the protocol depends on BOTH processes
    computing the identical path (the script stages, the service reads):
    the suffix is the channel's constant, the base is the translated
    store path, and neither composition root repeats the derivation.
    """
    return xbmcvfs.translatePath(STORE_PATH) + IMPORT_SUFFIX


class Settings:
    """Typed access to the addon settings store over Kodi's live proxy."""

    def __init__(self, *, log):
        """``log`` is a REQUIRED ``(message, level)`` sink (same convention as
        ``KodiGateway``)."""
        self._log = log
        # The Addon must outlive the Settings proxy (see LIFETIME RULE above).
        self._addon = xbmcaddon.Addon(ADDON_ID)
        self._settings = self._addon.getSettings()

    # --- typed primitives ---------------------------------------------------

    def get_bool(self, setting_id, default=False):
        """Read a boolean setting; on ANY error, log and return ``default``."""
        try:
            return self._settings.getBool(setting_id)
        except Exception:
            self._log(
                f"AOMe_Settings: Error getting boolean setting '{setting_id}'. "
                f"Using default: {default}", xbmc.LOGWARNING)
            return default

    def get_int(self, setting_id, default=0):
        """Read an integer setting; on ANY error, log and return ``default``."""
        try:
            return self._settings.getInt(setting_id)
        except Exception:
            self._log(
                f"AOMe_Settings: Error getting integer setting '{setting_id}'. "
                f"Using default: {default}", xbmc.LOGWARNING)
            return default

    def get_string_list(self, setting_id):
        """Read a list-of-strings setting; on ANY error, log and return ``[]``.

        The empty-list fallback reads as "no options selected", which is
        every list setting's do-nothing state — same fail-quiet doctrine
        as the other primitives.
        """
        try:
            return list(self._settings.getStringList(setting_id))
        except Exception:
            self._log(
                f"AOMe_Settings: Error getting string list setting "
                f"'{setting_id}'. Using default: []", xbmc.LOGWARNING)
            return []

    def store_boolean_if_changed(self, setting_id, value):
        """Write a boolean only if it differs from the stored value.

        Returns True when the store succeeds or is skipped (already equal);
        False when the underlying write raises. NOTE the pre-read runs
        through get_bool, which swallows read errors into the default — so a
        failed read of a setting whose target value equals that default
        skips the write and reports success (exact legacy semantics; reads
        of valid ids do not fail in practice).
        """
        if self.get_bool(setting_id) == value:
            return True
        return self._store(self._settings.setBool, setting_id, value, "boolean")

    def store_integer_if_changed(self, setting_id, value):
        """Write an integer only if it differs from the stored value.

        Returns True when the store succeeds or is skipped (already equal);
        False when the underlying write raises. See store_boolean_if_changed
        for the pre-read-vs-default caveat (legacy semantics).
        """
        if self.get_int(setting_id) == value:
            return True
        return self._store(self._settings.setInt, setting_id, value, "integer")

    def _store(self, operation, setting_id, value, value_type):
        """Log the store at LOGDEBUG and write; on error log LOGWARNING."""
        try:
            self._log(
                f"AOMe_Settings: Storing {value_type} setting {setting_id}: "
                f"{value}", xbmc.LOGDEBUG)
            operation(setting_id, value)
            return True
        except Exception:
            self._log(
                f"AOMe_Settings: Error storing {value_type} setting "
                f"'{setting_id}'.", xbmc.LOGWARNING)
            return False

    # --- intent-level reads (behavior settings only — offsets live in the
    # sparse store, never here; P4) --------------------------------------------

    def per_fps_offsets_enabled(self):
        """The ONE fps-granularity knob (D2/D3): OFF = the `all` key world."""
        return self.get_bool('per_fps_offsets')

    def apply_enabled(self):
        """The apply toggle (D9, amended in the beta9 field pass).

        D9's single global pause replaced classic's per-HDR enables; the
        field pass then split it into orthogonal Learn/Apply toggles and
        this is the apply half — it gates the applier ONLY, never the
        watcher. Defaults ON like the learn toggle: applying is the
        product, so an unreadable setting must never silently disable it.
        """
        return self.get_bool('apply_offsets', True)

    def remember_adjustments_enabled(self):
        """The learn loop's opt-out (classic 'active monitoring', promoted
        to core — P2). Defaults ON: learning is the product, so an
        unreadable setting must never silently disable it."""
        return self.get_bool('remember_adjustments', True)

    def seek_back_config(self, reason):
        """Return ``(enabled, seconds)`` for a seek-back reason; seconds >= 0.

        enabled = membership in the ``seek_back_events`` multiselect,
        whose option values are the SeekScheduler REASONS verbatim (the
        settings contract test pins that); the amount is the one shared
        slider. An unreadable read yields 0, which the scheduler treats
        as disabled (legacy fail-quiet parity).
        """
        return (reason in self.get_string_list('seek_back_events'),
                max(self.get_int('seek_back_seconds'), 0))

    def notify_apply_enabled(self):
        """The 'offset applied' toast gate (D10: each toast kind has its own
        toggle). Defaults ON — the toasts are the learn loop's teaching
        surface, so an unreadable setting must never silently mute them."""
        return self.get_bool('notify_apply', True)

    def notify_learn_enabled(self):
        """The 'offset saved' toast gate (D10; see notify_apply_enabled)."""
        return self.get_bool('notify_learn', True)

    def notification_duration_ms(self):
        return self.get_int('notification_seconds', 5) * 1000

    def debug_logging_enabled(self):
        return self.get_bool('enable_debug_logging')

    def coexistence_warned(self):
        """The classic-AOM coexistence warning's once-flag (§3.6).

        Behavior STATE, not offset data — a hidden level-4 bool in
        settings.xml, written through store_boolean_if_changed after the
        warning actually shows (its first runtime caller)."""
        return self.get_bool('coexistence_warned')

# (The OffsetTable adapter moved to aom/store/table.py — it stopped being a
# Kodi-settings concern when offsets moved into the sparse store; E2 review.)
