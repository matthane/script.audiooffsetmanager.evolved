# CLAUDE.md

Guidance for working in this repo. Dev-only: `export-ignore`'d in
`.gitattributes`, never packaged.

## What this is

**`script.audiooffsetmanager.evolved` ("Audio Offset Manager: Evolved")** —
a Kodi service addon whose product is the learn loop: the user fixes
lipsync once by adjusting Kodi's audio offset during playback, and
Evolved remembers the value per stream profile (HDR type, frame rate,
audio format) and re-applies it automatically on every matching playback.
Seek-back replays, notifications, and a management view of everything
learned ride along.

`main` is the only line. The addon moved here from branch `evolved/1.0`
of the classic repo on 2026-07-18, full history preserved. The classic
addon (`script.audiooffsetmanager`, github.com/matthane/script.audiooffsetmanager
— the `classic` remote in the primary working copy) is maintenance-only;
its hotfixes are cherry-picked into this repo deliberately, never
auto-merged. Do not edit the classic repo from here.

**Status:** construction is complete and validated (full suite green,
field-verified on Windows Kodi 22); the `1.0.0~beta` train is in field
soak and the repo is preparing for release. **Releases are cut by
tagging:** a release tag gets a GitHub Release. Betas are published as
pre-releases (the `prereleased` event does not fire `submit.yml`);
publishing a normal stable Release fires `submit.yml`, which submits
the addon to Kodi's repo-scripts. The version keeps its `~` pre-release
form until the deliberate final bump, and `submit.yml` refuses any `~`
version as a hard backstop — bumping `addon.xml` to `1.0.0` is a
prerequisite of submission, not a side effect of tagging.

## Design doctrine (summarized here because the design docs never ship)

The full design rationale lives in EVOLVED.md / EVOLVED-IMPLEMENTATION.md /
EVOLVED-UI.md at the repo root of the **primary working copy only**
(git-excluded via `.git/info/exclude`; they never materialize in agent
worktrees — reference them by absolute path). The load-bearing
principles, so this file stands alone:

- **Zero-config install.** No onboarding, no test video, no capability
  probe, no `new_install` flag, no stored platform flags. An empty store
  simply does nothing until taught; the save/apply toasts ARE the
  tutorial.
- **The remembered adjustment IS the product.** The editor is whatever
  changes Kodi's audio offset during playback — the native slider, a
  keymap, JSON-RPC, anything; the watcher reads the resulting value and
  never cares how it was set. Everything else is trim around that loop.
- **Capability gating is emergent, not probed.** The management view
  lists only profiles the platform actually produced; nothing is hidden
  or gated by stored capability state.
- **Settings are behavior; data is data.** The dialog holds ~14 behavior
  knobs; learned offsets live in the JSON store. No runtime state hides
  in settings.xml — the classic dialog-clobber hazard class is deleted,
  not managed.
- **Offsets are authored where they can be judged: during playback.**
  No surface outside playback ever enters or edits a millisecond value.
  The management view is inspection + delete/clear; export/import
  transports learned values without anyone typing one.
- **Classic's failure mode was overwhelming users; Evolved's is being
  invisible.** The notification defaults (both ON) and the addon
  description carry the teaching load — protect them.

## Entry points

- `service.py` → `aome.runtime.ServiceRuntime` — the composition root:
  builds the full dependency graph with required constructor injection
  and blocks on the abort monitor. Subscription order is load-bearing
  (tracker → detector → applier → notifier → seek scheduler → watcher);
  the runtime docstring is the authority.
- `script.py` → `aome.script_router` — the `RunScript` half, a separate
  process. Routes: `manage_offsets`, `export_offsets` / `import_offsets`,
  `export_log`; anything else opens the settings dialog (launching the
  addon lands on settings — the hub; every view returns there on exit).

## Component map

`resources/lib/aome/` — the module docstrings are the architecture
documentation; trust them first. Layering (enforced by
`tests/contract/test_architecture.py`):

- **`domain/`** — pure decisions, no Kodi, no I/O: `profile` (immutable
  verbatim stream facts), `stream_state` (STARTING → STABILIZING → STABLE
  machine), `policies` (gating, completeness, delay parsing, seek
  quiet-window), `formats` (the UNKNOWN absence sentinel only).
- **`store/`** — the sparse offset database, pure (path injected):
  `offset_store` (persistence, atomicity, corruption recovery, reset
  markers), `keys` (key codec + the one display-name table),
  `resolve` (**the key-schema decision table, executable** — lookup and
  write-key semantics live here), `table` (`OffsetTable`, the seam the
  pipeline speaks to).
- **`app/`** — orchestration on the dispatcher thread, pure:
  `dispatcher` (single-threaded event loop + timer scheduler — all state
  lives on this thread, no locks anywhere above it), `events` (typed
  frozen dataclasses), `session` (`PlaybackSession` owns ALL
  per-playback state; a new session IS the reset), `stream_detector`
  (scheduled probe/verify chain, sole writer of `session.profile`),
  `offset_applier`, `adjustment_watcher` (the learn loop),
  `seek_scheduler`, `notifier`, `store_mutations` (service side of the
  mutation channel).
- **`kodi/`** — the only package allowed to `import xbmc*`: `gateway`
  (single-shot JSON-RPC — patience lives up in scheduled events, never
  sleeps here), `settings`, `gui`, `log`, `player_bridge` /
  `monitor_bridge` (zero-logic posts to the dispatcher), `announce`
  (shared NotifyAll envelope), `mutation_client` (script-process side).
- **`view/`** — script-process surfaces over the store: `manage`
  (inspection + delete/clear), `transfer` (backup export/import),
  `logexport` (filtered, redacted support log).

Script-process-only files (`script_router`, `view/*`, the store read
path) deploy to a test box by file copy with NO Kodi restart;
service-side changes need a restart.

## The offset store doctrine

Offsets live in a **sparse JSON store**
(`special://profile/addon_data/script.audiooffsetmanager.evolved/offsets.json`),
never in settings.xml.

- **Single writer:** the dispatcher thread — the ONLY thing that writes
  the store file, ever. Atomic persistence (temp + fsync +
  `os.replace`), `.bad` rename on corruption (start empty, log loudly,
  notify once), versioned schema; a NEWER-schema file makes the store
  read-only rather than risk clobbering it.
- **Key schema** `hdr|fps|audio` — **verbatim acceptance**: the reported
  string, case-folded and trimmed, IS the key segment. No whitelist, no
  substring matching; the only alias is the proven `hlghdr` → `hlg`
  (empty HDR → `sdr` is the detector's chain-of-evidence call, not the
  key codec's). Do NOT add speculative aliases — only observed field
  fragmentation justifies one. `fps` is `all` (default) or the
  integer-truncated reported fps when the global `per_fps_offsets`
  toggle is ON; truncation is what keeps the NTSC fractional rates
  distinct from their integer siblings (23.976→`23` vs 24.0→`24` …) —
  the pairs are pinned in tests and must stay distinct.
- **Lookup:** `exact → all → miss` (toggle ON) or `all → miss` (OFF);
  both levels are single keys, so no scan and no tie-break exists.
  Specific-fps entries are dormant while the toggle is OFF; flipping it
  is non-destructive both ways. **`aome/store/resolve.py` is the
  decision table, executable** — its docstring plus
  `tests/unit/test_store_resolve.py` / `test_store_keys.py` are the spec.
- **Miss semantics:** a miss applies nothing UNTIL the addon has acted
  on the session, then it zero-resets stale residue (silent for our own,
  "Offset not saved" toast when discarding an unstored manual value).
  `delete`/`clear` leave **reset markers** that force 0 at the next
  resolve regardless — silently (the deletion is the authorization; a
  confirmation toast was tried and removed, do not reintroduce it).
- **Write rule — zero history-dependence:** the watcher always writes
  the single key derived at store instant from the current profile + the
  current toggle, NEVER conditional on what the lookup hit. This is the
  sparse-store form of the classic stale-key doctrine.
- **Immediate effect:** a settings save and a store-changing mutation
  are resolve moments — the applier re-runs its decision for the live
  session, so mid-playback edits act at once. A save never wipes the
  user's hand (only our own orphaned residue is reset).
- **Increment/range guarantee:** `delay_ms` is the verbatim signed
  integer Kodi reports, 1 ms resolution, bounded only by Kodi's ±10 s.
  Never reintroduce step (25 ms/5 ms) or range assumptions anywhere —
  store, views, tests, or any future nudge UI. Custom-build sliders just
  work because nothing quantizes.

## The mutation channel (two processes, one writer)

The management/transfer views run in the script process and **never
write the store file**. They read it through the read-only reader and
mutate over a `JSONRPC.NotifyAll` channel to the service, whitelisted to
`delete` / `clear` / `import` — no `set` op and no value field exists, so
the channel structurally cannot carry a value write. Acks are
request_id-matched; no ack means "service not running", reported to the
user with deliberately NO fallback write path. `import` is the backup
restore: replace-all semantics, values only ever from a staged backup
file (`offsets.json.import` — the one sibling file the script process
may write), no path and no value on the wire, service re-validates and
refuses an empty stage. Do not shortcut any of this with "the script
just edits the JSON."

## Settings doctrine (behavior settings only)

`settings.xml` (~180 lines, 14 settings) holds behavior knobs only:
`remember_adjustments` / `apply_offsets` (the orthogonal learn/apply
pair — the watcher never consults the apply toggle, so apply-off +
learn-on is the legal re-teach state), `per_fps_offsets`, the
`seek_back_events` multiselect (option values = `SeekScheduler.REASONS`
verbatim, contract-test-pinned) + shared `seek_back_seconds`,
`notify_apply` / `notify_learn` / `notification_seconds`,
`enable_debug_logging`, the action buttons, and the hidden
`coexistence_warned` once-flag. The classic settings-state doctrine
survives for these, unchanged and binding:

- The settings object is a **live shared proxy**, not a snapshot; it is
  live only while its parent `xbmcaddon.Addon` stays alive — keep the
  `Addon` on `self`, never `xbmcaddon.Addon(...).getSettings()` a
  temporary.
- **Never write a setting from Python while the settings dialog is open**
  (its save-on-close clobbers you); action buttons that lead anywhere
  use `<close>true</close>` and let the write settle. Service-side
  writes go through the `store_*_if_changed` helpers.
- Offset **data** is exempt from all of this because it never lives in
  settings — that hazard class is deleted, not managed.

## Kodi packaging constraints

- Only shippable files are tracked without `export-ignore`; dev tooling
  (this file, `.github/`, `tests/`, `tools/`, `.claude/`) is excluded.
  Verify anytime: `git archive --format=zip -o /tmp/pkg.zip HEAD` and
  inspect. The package is `aome/` + slim XML + resources under the
  dotted id.
- Requires `xbmc.python` **3.0.1** (Kodi Nexus floor → Python 3.8
  syntax everywhere).
- `addon.xml` `<news>` is schema-capped at 1500 characters — keep only
  the last couple of versions; older changelogs live in git history.
- CI (`ci.yml`) is **manual-dispatch only** — the local pytest suite
  gates every commit, so CI runs as part of cutting a beta:
  `gh workflow run ci.yml --ref main`, then `gh run watch`.

## Conventions

- Match existing style: module docstrings state design intent and
  behavior (never construction history), constructor dependency
  injection with required args, no globals, no singletons; runtime code
  under `resources/lib/aome/`.
- Kodi I/O through the gateway adapters only; logging through the
  injected sinks with `AOMe_`-prefixed messages (the `e` distinguishes
  Evolved's lines from classic AOM's in a shared kodi.log). The debug
  toggle escalates the addon's LOGDEBUG lines to LOGINFO; the Advanced
  "Export addon log" button produces the redacted support report.
- `strings.po`: retired string ids are never reused; new strings get
  translator context comments; contract tests pin id parity and
  fallback defaults.
- User-facing text: plain and succinct, describe the behavior and the
  off-state, no em dashes, no marketing framing.
- Small imperative commits with the
  `Co-authored-by: Claude <noreply@anthropic.com>` trailer; full pytest
  green (984 tests, seconds to run: `python -m pytest tests -q`) before
  every commit.
