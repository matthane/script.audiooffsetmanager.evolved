"""Contract test: aome layering rules (import purity).

Layering: `aome.domain` is pure Python (no Kodi imports);
`aome.app` imports only stdlib + aome;
`aome.kodi` is the only package allowed to import xbmc*.
`aome/runtime.py` and `aome/script_router.py` are composition roots
(per-process entry glue) and exempt by design (they sit outside the
checked subpackages).
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AOME = REPO_ROOT / "resources" / "lib" / "aome"

# The ONLY aome.app modules allowed to import xbmc* or non-aome
# resources.lib code. Empty: every app module is pure.
APP_IMPURITY_EXEMPTIONS = set()


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _py_files(folder):
    files = sorted(folder.glob("*.py"))
    assert files, "no python files found under {0}".format(folder)
    return files


def test_domain_is_pure():
    for path in _py_files(AOME / "domain"):
        for name in _imports(path):
            assert not name.startswith("xbmc"), \
                "{0} imports Kodi module {1}".format(path.name, name)
            if name.startswith("resources.lib."):
                assert name.startswith("resources.lib.aome.domain"), \
                    "{0} imports outside the domain layer: {1}".format(path.name, name)


def test_app_is_pure_except_explicit_exemptions():
    for path in _py_files(AOME / "app"):
        if path.name in APP_IMPURITY_EXEMPTIONS:
            continue
        for name in _imports(path):
            assert not name.startswith("xbmc"), \
                "{0} imports Kodi module {1}".format(path.name, name)
            if name.startswith("resources.lib."):
                assert name.startswith("resources.lib.aome."), \
                    "{0} imports non-aome module {1}".format(path.name, name)


def test_app_exemption_list_is_exact():
    # A deleted shim must not leave a stale exemption behind.
    app_files = {p.name for p in (AOME / "app").glob("*.py")}
    missing = APP_IMPURITY_EXEMPTIONS - app_files
    assert not missing, \
        "stale APP_IMPURITY_EXEMPTIONS entries for deleted files: {0}".format(missing)


def _in_packages(name, packages):
    # Package-boundary check: bare startswith would also match sibling
    # modules that merely share the prefix (aome.domainx, aome.store_adapters).
    return any(name == pkg or name.startswith(pkg + ".") for pkg in packages)


def test_store_is_pure():
    # The sparse offset store is pure Python: the file path is injected, so
    # xbmcvfs never enters; it may lean on the domain layer (formats
    # constants) and itself, nothing else.
    allowed = ("resources.lib.aome.domain", "resources.lib.aome.store")
    for path in _py_files(AOME / "store"):
        for name in _imports(path):
            assert not name.startswith("xbmc"), \
                "{0} imports Kodi module {1}".format(path.name, name)
            if name.startswith("resources.lib."):
                assert _in_packages(name, allowed), \
                    "{0} imports outside domain/store: {1}".format(path.name, name)


def test_view_is_pure():
    # The management view runs in the script process but is itself pure Python:
    # every Kodi touch is an injected seam (gui, reader, mutation channel), so
    # xbmc* never enters. It may lean on the domain and store layers (the
    # read-only reader, key display helpers) and itself, nothing else.
    allowed = (
        "resources.lib.aome.domain",
        "resources.lib.aome.store",
        "resources.lib.aome.view",
    )
    for path in _py_files(AOME / "view"):
        for name in _imports(path):
            assert not name.startswith("xbmc"), \
                "{0} imports Kodi module {1}".format(path.name, name)
            if name.startswith("resources.lib."):
                assert _in_packages(name, allowed), \
                    "{0} imports outside domain/store/view: {1}".format(path.name, name)


def test_kodi_adapters_import_only_aom_and_kodi():
    for path in _py_files(AOME / "kodi"):
        for name in _imports(path):
            if name.startswith("resources.lib."):
                assert name.startswith("resources.lib.aome."), \
                    "{0} imports non-aome module {1}".format(path.name, name)
