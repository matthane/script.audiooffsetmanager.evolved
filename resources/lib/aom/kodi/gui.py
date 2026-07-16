"""Kodi GUI toast surface — the only place xbmcgui notifications are raised.

Single-shot BY DESIGN, matching the gateway convention: each method performs
exactly one Kodi GUI call and returns, guarded so a transient GUI-layer
failure degrades to a log line rather than unwinding the caller mid-handler.

String resolution goes through ``getLocalizedString`` rather than the legacy
``$ADDON[<addon-id> <id>]`` label macros the NotificationHandler
used — a Phase 7 work item: the app-layer Notifier now owns message assembly
and hands this surface a fully-resolved string, so the macro indirection is
gone.

This is an ``aom.kodi`` adapter: the only aom layer permitted to import
``xbmcgui``/``xbmcaddon``.
"""

import xbmc
import xbmcaddon
import xbmcgui

from resources.lib.aom.kodi.settings import ADDON_ID


class Gui:
    """Single-shot wrapper over Kodi's notification dialog and string table."""

    def __init__(self, *, log):
        """``log`` is a REQUIRED ``(message, level)`` sink — production
        injects the ``aom.kodi.log.KodiLogger`` callable, matching the
        gateway convention, so one logger instance (and its cached debug
        escalation) serves the whole process.
        """
        addon = xbmcaddon.Addon(ADDON_ID)
        self._addon = addon
        self._name = addon.getAddonInfo('name')
        self._icon = addon.getAddonInfo('icon')
        self._log = log

    def localized(self, string_id):
        """Return the localized string for ``string_id`` ('' on failure).

        The exception guard mirrors the gateway's reads: a transient string
        lookup failure yields the empty sentinel instead of unwinding the
        caller's message assembly.
        """
        try:
            return self._addon.getLocalizedString(string_id)
        except Exception as e:
            self._log(f"AOMe_Gui: Error reading localized string {string_id}: "
                      f"{str(e)}", xbmc.LOGERROR)
            return ''

    def select(self, heading, options):
        """Show a selection list; return the chosen index, -1 on cancel/error.

        The management view's list surface (D6: plain dialogs). -1 (Kodi's
        cancel value) doubles as the error fallback so a transient GUI
        failure reads as "user backed out" rather than unwinding the view.
        """
        try:
            return xbmcgui.Dialog().select(heading, options)
        except Exception as e:
            self._log(f"AOMe_Gui: Error showing select dialog: {str(e)}",
                      xbmc.LOGERROR)
            return -1

    def yesno(self, heading, message):
        """Show a yes/no confirmation; return True only on an explicit yes.

        The error fallback is False — a transient GUI failure must never
        read as consent (the view uses this to confirm delete/clear).
        """
        try:
            return bool(xbmcgui.Dialog().yesno(heading, message))
        except Exception as e:
            self._log(f"AOMe_Gui: Error showing yesno dialog: {str(e)}",
                      xbmc.LOGERROR)
            return False

    def ok(self, heading, message):
        """Show a modal OK dialog; True when it actually rendered.

        The bool matters to callers that gate a side effect on the user
        having SEEN the dialog (the coexistence once-flag, E4 review): a
        swallowed GUI failure returns False so the caller can retry later
        instead of marking an unshown warning as shown.
        """
        try:
            xbmcgui.Dialog().ok(heading, message)
            return True
        except Exception as e:
            self._log(f"AOMe_Gui: Error showing ok dialog: {str(e)}",
                      xbmc.LOGERROR)
            return False

    def notification(self, message, duration_ms, title=None, icon=None):
        """Raise one Kodi toast for ``message`` lasting ``duration_ms``.

        ``title`` and ``icon`` default to the addon's name and icon; callers
        may override them (e.g. ``xbmcgui.NOTIFICATION_ERROR`` toasts).
        Exception guard: a GUI-layer failure logs LOGERROR through the sink
        and never unwinds the caller.
        """
        try:
            xbmcgui.Dialog().notification(
                title if title is not None else self._name,
                message,
                icon if icon is not None else self._icon,
                duration_ms)
        except Exception as e:
            self._log(f"AOMe_Gui: Error raising notification: {str(e)}",
                      xbmc.LOGERROR)
