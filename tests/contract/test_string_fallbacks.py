"""Contract test: view fallback strings mirror the strings.po source text.

The views keep English fallbacks for strings that must never render blank
(``localized()`` degrades to '' on a transient failure). Each fallback is a
COPY of the en_gb msgid, and a copy can drift: a wording or placeholder edit
on one side that misses the other ships two different strings for the same
id — including a placeholder mismatch that would silently swallow a value.
This pins every ``_FALLBACKS`` entry to its msgid verbatim, so the pair can
only ever change together.

Scope: the dict-based fallbacks in the view modules (manage, transfer,
logexport) and the app-layer per-fps advisor. The service-side inline
fallbacks (notifier, runtime) carry no placeholders and are not covered
here.
"""

import re
from pathlib import Path

import pytest

from resources.lib.aome.app import per_fps_advisor
from resources.lib.aome.view import logexport, manage, transfer

REPO_ROOT = Path(__file__).resolve().parents[2]
STRINGS_PO = (REPO_ROOT / "resources" / "language"
              / "resource.language.en_gb" / "strings.po")

# msgid on a single line, as the whole file is written today. A future
# multi-line msgid would parse as ABSENT and fail the mirror test loudly
# (never silently pass), telling whoever hits it to widen this parser.
_PO_ENTRY_RE = re.compile(r'msgctxt "#(\d+)"\nmsgid "(.*)"\nmsgstr')


def _po_msgids():
    text = STRINGS_PO.read_text(encoding="utf-8")
    return {int(string_id): msgid
            for string_id, msgid in _PO_ENTRY_RE.findall(text)}


PO_MSGIDS = _po_msgids()

# (view name, string id, fallback text) for every fallback entry. A set
# first: transfer deliberately shares some of manage's entries, and the
# shared pair must not parametrize twice under one id.
FALLBACK_CASES = sorted(
    {(module.__name__.rsplit(".", 1)[-1], string_id, fallback)
     for module in (manage, transfer, logexport, per_fps_advisor)
     for string_id, fallback in module._FALLBACKS.items()})


def test_fallback_collector_is_non_empty():
    # Guard: an import/refactor that emptied the collection would make the
    # mirror test below pass vacuously.
    assert FALLBACK_CASES, "no _FALLBACKS entries collected from the views"


@pytest.mark.parametrize(
    "view, string_id, fallback", FALLBACK_CASES,
    ids=["{0}-{1}".format(view, string_id)
         for view, string_id, _ in FALLBACK_CASES])
def test_fallback_mirrors_po_msgid(view, string_id, fallback):
    msgid = PO_MSGIDS.get(string_id)
    assert msgid is not None, (
        "{0}._FALLBACKS[{1}] has no single-line msgid entry in strings.po"
        .format(view, string_id))
    assert fallback == msgid, (
        "{0}._FALLBACKS[{1}] drifted from the strings.po msgid\n"
        "  po: {2!r}\n  fb: {3!r}".format(view, string_id, msgid, fallback))
