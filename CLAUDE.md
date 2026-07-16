# CLAUDE.md

Guidance for working on this branch. Dev-only: `export-ignore`'d in
`.gitattributes`, never packaged.

## What this is

**Branch `evolved/1.0` builds a different addon than `main` does:**
`script.audiooffsetmanagerevolved` ("AOM Evolved") — a from-the-2.0-core
rework of Audio Offset Manager whose product is the learn loop: the user
fixes lipsync once with Kodi's native audio-offset slider during playback,
and Evolved remembers the value per stream profile (HDR type, audio format,
refresh rate) and re-applies it automatically forever after.

The classic addon (`script.audiooffsetmanager`) lives on `main` /
`redesign/2.0` and is maintenance-only; its hotfixes are cherry-picked onto
this branch deliberately, never auto-merged. Do not edit classic branches
from here.

## CONSTRUCTION IN PROGRESS (Phases E0–E8)

This branch is being built per **EVOLVED.md** (design contract) and
**EVOLVED-IMPLEMENTATION.md** (execution plan) — both at the repo root,
**git-excluded via `.git/info/exclude`** (they exist only in the primary
working copy and never materialize in agent worktrees; reference them by
absolute path). Currently completing **Phase E0** (identity, triage,
decisions). Until E2 lands, the runtime still runs classic's settings-backed
`OffsetTable`; the sections below describe the TARGET state where they
differ. Trust the `aom/` module docstrings and the two local docs over any
stale statement here — this file gets its full rewrite in E8.

## Kodi packaging constraints

- Same doctrine as classic: only shippable files tracked without
  `export-ignore`; dev tooling (this file, `.github/`, `tests/`, `tools/`,
  `.claude/`) is excluded. Verify anytime:
  `git archive --format=zip -o /tmp/pkg.zip HEAD` and inspect.
- Requires `xbmc.python` **3.0.1** (Kodi Nexus, Python 3.8 syntax — D8).
- **No GitHub releases are ever created from this branch** (publishing is
  a separate future plan). Betas are local zips via `git archive`. The
  version always carries `~` or pre-1.0 form, so `submit.yml`'s stable-
  release trigger structurally cannot fire.

## Entry points

Unchanged from 2.0: `service.py` → `aom.runtime` (the dispatcher-owned
service), `script.py` → the script-process router (settings, and from E4
the `manage_offsets` management view).

## Architecture

The 2.0 backend is inherited whole: single-threaded dispatcher owning all
state, per-playback `PlaybackSession` with the STARTING/STABILIZING/STABLE
stream-state machine, `aom/domain|app|kodi` layering (domain/app are pure —
the architecture contract test enforces it), scheduled probe/verify stream
detection, seek scheduler with PM4K coordination, deferral-based notifier.
See the `aom/` module docstrings — they are the architecture documentation.

**What Evolved replaces (the offset data model):**

- Offsets live in a **sparse JSON store**
  (`special://profile/addon_data/script.audiooffsetmanagerevolved/offsets.json`),
  never in settings.xml. Written ONLY by the dispatcher thread (single
  writer). Atomic persistence (temp + `os.replace`), `.bad` corruption
  recovery, versioned schema.
- **Key schema** `hdr|fps|audio` (EVOLVED.md §3.2 — the decision table is
  the test spec): open vocabulary on all axes with a normalization layer;
  fps segment is `all` (default) or integer-truncated reported fps when the
  global `per_fps_offsets` toggle is ON. Lookup: `exact → all → miss`
  (toggle ON) or `all → miss` (OFF); miss = no-op. The watcher always
  writes the key derived at store instant from the current profile +
  toggle — never conditional on lookup history.
- **Increment/range guarantee:** `delay_ms` is the verbatim signed integer
  Kodi reports, 1 ms resolution, bounded only by Kodi's ±10 s. Never
  reintroduce step (25 ms/5 ms) or range assumptions anywhere.
- The management view (script process) is inspection + delete/clear only
  (P6 — no value entry anywhere outside playback); its mutations reach the
  service over a `NotifyAll` channel whitelisted to delete/clear. The
  script process NEVER writes the store file.
- No onboarding, no test video, no `new_install`, no stored platform
  capability flags — capability gating is emergent from the store (P3).

## Settings doctrine (behavior settings only, post-E3)

`settings.xml` holds ~15 behavior knobs (pause, remember-adjustments,
`per_fps_offsets`, seek-back block, notifications, debug). The classic
settings-state doctrine survives for these, unchanged and binding:

- The settings object is a **live shared proxy**, not a snapshot; keep the
  parent `Addon` alive for its lifetime.
- **Never write a setting from Python while the settings dialog is open**
  (its save-on-close clobbers you); action buttons that lead to writes use
  `<close>true</close>` and let the write settle.
- Offset **data** is exempt from all of this because it never lives in
  settings — that hazard class is deleted, not managed (P4).

## Conventions

- Match existing style: module docstrings, constructor dependency
  injection, no globals; new runtime files under `resources/lib/aom/`.
- Kodi I/O through the gateway adapters; logging through the injected
  sinks with `AOM_`-prefixed messages; Python 3.8 syntax.
- Small imperative commits with the
  `Co-authored-by: Claude <noreply@anthropic.com>` trailer; annotated
  `evolved-N` tags at phase boundaries; full pytest green before every
  commit.
