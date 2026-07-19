"""Unit tests for aome.app.per_fps_advisor (PerFpsAdvisor).

The advisor is a flip detector: it caches the per_fps toggle at
construction, compares on every SettingsChanged, and presents ONE
direction-specific explainer per flip through the injected presenter.
Everything here pins that contract: no fire at construction, no fire on
unrelated saves, one fire per flip with the right body, and English
fallbacks when localized() degrades.
"""

import pytest

from resources.lib.aome.app import events
from resources.lib.aome.app.dispatcher import Dispatcher
from resources.lib.aome.app.per_fps_advisor import (PerFpsAdvisor,
                                                   STRING_DISABLED_BODY,
                                                   STRING_ENABLED_BODY,
                                                   STRING_HEADING)
from tests.fakes import FakeClock, FakeGui


class FakeSettings:
    """The advisor's whole settings read surface: the per_fps toggle."""

    def __init__(self, per_fps=False):
        self.per_fps = per_fps

    def per_fps_offsets_enabled(self):
        return self.per_fps


class Rig:
    def __init__(self, per_fps=False):
        self.errors = []
        self.debug = []
        self.dispatcher = Dispatcher(clock=FakeClock(),
                                     log_error=self.errors.append,
                                     log_debug=self.debug.append)
        self.settings = FakeSettings(per_fps=per_fps)
        self.gui = FakeGui()
        self.presented = []              # (heading, body) per present call
        self.advisor = PerFpsAdvisor(
            self.dispatcher, self.settings, self.gui,
            present=lambda heading, body: self.presented.append(
                (heading, body)),
            log_debug=self.debug.append)

    def save_settings(self, per_fps=None):
        """One settings-dialog save: optionally flip the toggle, then post."""
        if per_fps is not None:
            self.settings.per_fps = per_fps
        self.dispatcher.post(events.SettingsChanged())
        self.dispatcher.run_pending()


@pytest.fixture
def rig():
    return Rig()


def test_construction_never_presents(rig):
    # Seeding the cached value at construction means a service (re)start
    # can never read as a flip — whatever the toggle already is.
    assert rig.presented == []
    assert Rig(per_fps=True).presented == []


def test_unrelated_save_presents_nothing(rig):
    rig.save_settings()                  # same value: not a flip
    rig.save_settings()
    assert rig.presented == []


def test_flip_on_presents_the_enabled_explainer_once(rig):
    rig.save_settings(per_fps=True)

    assert rig.presented == [
        (f"#{STRING_HEADING}", f"#{STRING_ENABLED_BODY}")]
    # The save that follows without a flip is quiet — one dialog per flip.
    rig.save_settings()
    assert len(rig.presented) == 1
    assert any('turned on' in line for line in rig.debug)


def test_flip_off_presents_the_disabled_explainer(rig):
    rig.save_settings(per_fps=True)
    rig.save_settings(per_fps=False)

    assert rig.presented[-1] == (
        f"#{STRING_HEADING}", f"#{STRING_DISABLED_BODY}")
    assert len(rig.presented) == 2       # one per flip, each direction
    assert any('turned off' in line for line in rig.debug)


def test_blank_localized_degrades_to_english_fallbacks(rig):
    # localized() returns '' on a transient failure; a blank explainer
    # teaches nothing, so the English source text must fill in (the
    # corruption/coexistence doctrine).
    rig.gui.localized = lambda _string_id: ''

    rig.save_settings(per_fps=True)
    heading, body = rig.presented[0]
    assert heading == "Per-frame-rate offsets"
    assert "will not be applied while it is on" in body

    rig.save_settings(per_fps=False)
    _heading, body = rig.presented[1]
    assert "turned on again" in body
