"""Per-fps mode explainer — a modal dialog when the toggle flips.

Flipping ``per_fps_offsets`` changes which stored offsets are in effect
(an offset applies only in the mode it was saved in), and without a word
the change is invisible until a playback resolves differently — the
addon's known failure mode. This component watches ``SettingsChanged``
for a FLIP of the toggle — the cached previous value against the live
read, so unrelated saves never fire and service start never fires — and
presents one explainer per flip, direction-specific: turning ON states
that all-rate offsets stop applying and each rate is taught during
playback; turning OFF states that the single all-rates offset is back
and per-rate offsets are kept dormant.

The dialog goes through the injected ``present(heading, body)``
callable, never raised here: a modal blocks its calling thread, and
this handler runs on the dispatcher thread, where every event and timer
is serialized. The runtime injects a presenter that hands the modal to
a short-lived display-only thread (no state crosses that seam, so the
single-threaded-state doctrine holds).

The heading reuses the toggle's own label (#32110) so the dialog names
the setting the user just touched. Both strings carry English fallbacks
(``localized()`` degrades to '' on a transient failure, and a blank
explainer teaches nothing — the corruption/coexistence doctrine).

Pure app layer: settings via the injected adapter, strings via the
injected gui, log sink injected; no Kodi imports.
"""

from resources.lib.aome.app import events

# The settings toggle's label doubles as the dialog heading.
STRING_HEADING = 32110
STRING_ENABLED_BODY = 32168
STRING_DISABLED_BODY = 32169

_FALLBACK_HEADING = "Per-frame-rate offsets"
_FALLBACK_ENABLED = (
    "Offsets are now learned and applied separately for each video frame "
    "rate. Offsets saved while this was off cover all frame rates and "
    "will not be applied while it is on. To teach a frame rate, adjust "
    "the audio offset once during playback.")
_FALLBACK_DISABLED = (
    "One offset now covers all frame rates of the same HDR type and "
    "audio format. Offsets saved for specific frame rates are kept, but "
    "will not be applied until per-frame-rate offsets are turned on "
    "again.")


class PerFpsAdvisor:
    """Presents the mode-change explainer when per_fps_offsets flips."""

    def __init__(self, dispatcher, settings, gui, present, *, log_debug):
        self._settings = settings
        self._gui = gui
        self._present = present
        self._log = log_debug
        # The value every future save is compared against; seeded at
        # construction so a service (re)start can never read as a flip.
        self._last = settings.per_fps_offsets_enabled()

        dispatcher.subscribe(events.SettingsChanged,
                             self._on_settings_changed)

    def _on_settings_changed(self, _event):
        now = self._settings.per_fps_offsets_enabled()
        if now == self._last:
            return
        self._last = now
        if now:
            body = self._gui.localized(STRING_ENABLED_BODY) or (
                _FALLBACK_ENABLED)
        else:
            body = self._gui.localized(STRING_DISABLED_BODY) or (
                _FALLBACK_DISABLED)
        heading = self._gui.localized(STRING_HEADING) or _FALLBACK_HEADING
        self._log(f"AOMe_PerFpsAdvisor: per-frame-rate offsets turned "
                  f"{'on' if now else 'off'}; presenting the explainer")
        self._present(heading, body)
