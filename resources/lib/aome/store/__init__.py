"""Sparse offset store: the dispatcher-owned JSON profile database.

Evolved keeps learned offsets here (addon_data/offsets.json), never in
settings.xml. Pure Python: the file path is injected, so xbmcvfs stays out
and the architecture purity contract applies to this subpackage.
"""
