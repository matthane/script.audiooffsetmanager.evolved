"""Contract test: the setting ids Python reads and settings.xml declares agree.

settings.xml is hand-written, so nothing mechanically keeps the declared knobs and the runtime's reads in
lockstep. A typo in an id, a setting declared but never wired up, or a read that
never got a control would all pass silently — Kodi returns the type default for
a missing id, so the addon would look "fine" while a knob did nothing.

This pins a two-way drift oracle:

  Direction A — every setting id the Python runtime reads is declared in
  settings.xml (otherwise the read silently returns a default forever).

  Direction B — every setting id declared in settings.xml is read by the
  runtime, except a small explicit whitelist of UI-only ids (an action button's
  RunScript wiring is not a settings read).

The Python-read set is collected by driving the REAL ``Settings`` class with its
typed primitives replaced by recorders and every intent-level accessor invoked —
including one ``seek_back_config`` call per ``SeekScheduler.REASONS`` entry (the
constant is imported, never hardcoded, so adding a reason widens the oracle
automatically). A completeness guard asserts the exercised accessor list equals
the class's public intent-read methods, so a newly added read cannot dodge the
oracle: the test fails until it is exercised here AND its id lands in
settings.xml.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

from resources.lib.aome.app.seek_scheduler import SeekScheduler
from resources.lib.aome.kodi.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_XML = REPO_ROOT / "resources" / "settings.xml"

# settings.xml ids that are intentionally not backed by a Python read: UI-only
# wiring. These are action buttons whose <data> is a RunScript invocation,
# not a value the runtime ever reads.
UI_ONLY_SETTING_IDS = {"manage_offsets", "export_offsets", "import_offsets",
                       "export_log"}

# The typed primitives and writers on Settings — everything that is NOT an
# intent-level read. The completeness guard subtracts these from the class's
# public surface; whatever remains must be exercised by this test.
NON_INTENT_CALLABLES = {
    "get_bool",
    "get_int",
    "get_string_list",
    "store_boolean_if_changed",
    "store_integer_if_changed",
}


def _settings_xml_ids():
    tree = ET.parse(str(SETTINGS_XML))
    return {
        element.get("id")
        for element in tree.iter("setting")
        if element.get("id")
    }


def _collect_python_read_ids():
    """Drive the real Settings class and record every id it reads.

    Returns ``(read_ids, exercised_methods)``: the set of setting ids touched
    and the set of intent-read method names invoked to touch them.
    """
    recorded = []
    settings = Settings(log=lambda *args, **kwargs: None)
    # Shadow the typed primitives with recorders (plain functions set as
    # instance attributes, so they take no ``self``). Return type-appropriate
    # values so accessors that post-process the read (max(), *1000) still run.
    settings.get_bool = lambda setting_id, default=False: (
        recorded.append(setting_id) or False
    )
    settings.get_int = lambda setting_id, default=0: (
        recorded.append(setting_id) or 0
    )
    settings.get_string_list = lambda setting_id: (
        recorded.append(setting_id) or []
    )

    exercised = set()

    def exercise(method_name, *args):
        exercised.add(method_name)
        getattr(settings, method_name)(*args)

    exercise("per_fps_offsets_enabled")
    exercise("apply_enabled")
    exercise("remember_adjustments_enabled")
    exercise("notify_apply_enabled")
    exercise("notify_learn_enabled")
    exercise("notification_duration_ms")
    exercise("debug_logging_enabled")
    exercise("coexistence_warned")
    for reason in SeekScheduler.REASONS:
        exercise("seek_back_config", reason)

    return set(recorded), exercised


PYTHON_READ_IDS, EXERCISED_METHODS = _collect_python_read_ids()
SETTINGS_XML_IDS = _settings_xml_ids()


def _public_intent_reads():
    """Public intent-read method names on Settings: all public callables minus
    the typed primitives/writers."""
    return {
        name
        for name in dir(Settings)
        if not name.startswith("_")
        and callable(getattr(Settings, name))
        and name not in NON_INTENT_CALLABLES
    }


def test_settings_xml_declares_ids():
    # Guard: an empty parse would make the membership tests pass vacuously.
    assert SETTINGS_XML_IDS, "no <setting id=...> collected from settings.xml"
    assert PYTHON_READ_IDS, "no ids recorded from the Settings intent reads"


def test_completeness_all_intent_reads_are_exercised():
    # If someone adds a new intent read to Settings, it must be exercised here
    # (which forces its id to be declared in settings.xml by Direction A). This
    # keeps the oracle from being silently bypassed.
    assert _public_intent_reads() == EXERCISED_METHODS, (
        "Settings intent-read methods have drifted from the exercised set; "
        "add the new read to this test (and declare its id in settings.xml)"
    )


def test_direction_a_every_python_read_id_is_declared():
    missing = PYTHON_READ_IDS - SETTINGS_XML_IDS
    assert not missing, (
        "Python reads setting ids absent from settings.xml: {0}".format(
            sorted(missing)
        )
    )


def test_direction_b_every_declared_id_is_read():
    unread = SETTINGS_XML_IDS - PYTHON_READ_IDS - UI_ONLY_SETTING_IDS
    assert not unread, (
        "settings.xml declares ids no Python read consumes (add a read or "
        "whitelist as UI-only): {0}".format(sorted(unread))
    )


def test_seek_back_option_values_match_scheduler_reasons():
    # The multiselect's option VALUES must be the scheduler's reason
    # vocabulary VERBATIM: seek_back_config is a membership test against
    # them, and nothing else would catch a rename — the fakes bypass the
    # real mapping, so a drifted value ('change' -> 'store', or a
    # well-meaning edit toward the display label) would silently disable
    # that seek forever with every suite green. The labels may say
    # anything; the values may not move.
    tree = ET.parse(str(SETTINGS_XML))
    options = [
        option.text
        for setting in tree.iter("setting")
        if setting.get("id") == "seek_back_events"
        for option in setting.iter("option")
    ]
    assert sorted(options) == sorted(SeekScheduler.REASONS)
