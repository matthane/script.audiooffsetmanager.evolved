# Audio Offset Manager: Evolved

Contributor guide for this repository. This file is developer-only
(`export-ignore`'d in `.gitattributes`), so it never ships in the addon
package.

## What it is

A Kodi service addon built around one loop: the user fixes lipsync once by
adjusting Kodi's audio offset during playback, and the addon remembers that
value per stream profile (HDR type, frame rate, audio format) and re-applies
it automatically on every matching playback. Seek-back replays,
notifications, and a view for managing everything learned are built around
that loop.

`main` is the only branch. An earlier addon, `script.audiooffsetmanager`
("classic"), is a separate, end-of-life project; nothing is shared or
cherry-picked between the two.

## Design principles

These shape most decisions in the codebase; keep them in mind when adding to
it.

- **Zero-config install.** No onboarding, test video, capability probe, or
  stored platform flags. An empty store does nothing until the user teaches
  it, and the save/apply toasts are the tutorial.
- **The remembered adjustment is the product.** The "editor" is whatever
  changes Kodi's audio offset during playback: the native slider, a keymap,
  JSON-RPC, anything. The watcher reads the resulting value and does not care
  how it was set. Everything else is trim around that loop.
- **Capability gating is emergent, not probed.** The management view lists
  only the profiles the platform actually produced. Nothing is hidden or
  gated by stored capability state.
- **Settings are behavior; data is data.** The settings dialog holds ~14
  behavior toggles; learned offsets live in a JSON store, never in
  settings.xml. Keeping runtime state out of settings.xml avoids a class of
  dialog save-on-close bugs.
- **Offsets are authored during playback, where they can be judged.** No
  surface outside playback enters or edits a millisecond value. The
  management view is inspection plus delete/clear; export/import moves
  learned values without anyone typing one.
- **The risk to guard against is invisibility.** The addon does its work
  silently, so the notification defaults (both on) and the addon description
  carry the teaching load.

## Entry points

Two processes share one file (the offset store):

- `service.py` → `aome.runtime.ServiceRuntime` is the long-running service.
  It builds the full dependency graph with required constructor injection and
  blocks on Kodi's abort monitor. Component subscription order matters
  (tracker → detector → applier → notifier → seek scheduler → watcher); the
  runtime module's docstring explains why.
- `script.py` → `aome.script_router` is the `RunScript` half, launched on
  demand. Routes: `manage_offsets`, `export_offsets` / `import_offsets`,
  `export_log`. Anything else opens the settings dialog, which is the hub
  every view returns to on exit.

## How the code is organized

All runtime code lives under `resources/lib/aome/`. Each module's docstring
describes its responsibility and invariants; read those first. The layering
is enforced by `tests/contract/test_architecture.py`:

- **`domain/`** — pure decisions, no Kodi and no I/O: `profile` (immutable
  stream facts), `stream_state` (the STARTING → STABILIZING → STABLE
  machine), `policies` (gating, completeness, delay parsing, the seek
  quiet-window rule), `formats` (the "unknown" absence sentinel).
- **`store/`** — the sparse offset database, pure (the file path is
  injected): `offset_store` (persistence, atomicity, corruption recovery,
  reset markers), `keys` (the key codec and the one display-name table),
  `resolve` (lookup and write-key semantics), `table` (`OffsetTable`, the
  adapter the pipeline talks to).
- **`app/`** — orchestration on the dispatcher thread, pure: `dispatcher`
  (a single-threaded event loop and timer scheduler; all state lives on this
  thread, so nothing above it needs locks), `events` (typed frozen
  dataclasses), `session` (`PlaybackSession` owns all per-playback state, and
  a new session is the reset), `stream_detector` (the probe/verify chain,
  sole writer of `session.profile`), `offset_applier`, `adjustment_watcher`
  (the learn loop), `seek_scheduler`, `notifier`, `store_mutations` (the
  service side of the mutation channel).
- **`kodi/`** — the only package allowed to `import xbmc*`: `gateway`
  (single-shot JSON-RPC; retries live in scheduled events, never as sleeps
  here), `settings`, `gui`, `log`, `player_bridge` / `monitor_bridge`
  (zero-logic posts to the dispatcher), `announce` (the shared NotifyAll
  envelope), `mutation_client` (the script-process side).
- **`view/`** — script-process surfaces over the store: `manage` (inspection
  + delete/clear), `transfer` (backup export/import), `logexport` (a
  filtered, redacted support log).

## The offset store

Learned offsets live in a sparse JSON file
(`special://profile/addon_data/script.audiooffsetmanager.evolved/offsets.json`),
never in settings.xml. The rules that keep it safe:

- **Single writer.** Only the service's dispatcher thread ever writes the
  file. Persistence is atomic (temp file, fsync, then `os.replace`); a file
  that fails to parse is renamed to `.bad` and the store starts empty (and
  notifies once); a file from a newer schema version makes the store
  read-only rather than risk overwriting it. The schema version is 2 (the
  channel axis grew the key to four segments); a version-1 file loads
  writable, its keys expanding through boundary canonicalization — that
  expansion IS the migration, with no separate migration code — and the
  first persist rewrites the file as v2.
- **Keys are `hdr|fps|audio|ch`, accepted verbatim.** The reported string,
  case-folded and trimmed, is the key segment. There is no whitelist and no
  substring matching, so a codec or HDR type this code has never seen still
  works. The HDR axis also strips internal whitespace and carries a small set
  of cross-build aliases (`hlghdr` → `hlg`, `hdr10+` → `hdr10plus`) for
  formats Kodi 21 and 22 spell differently; add an alias only for a spelling
  split actually observed, not speculatively. An absent HDR reading resolves
  to `sdr` in the detector's chain-of-evidence, not in the key codec. The
  store canonicalizes every key at its boundary, so entries written under
  older spellings keep resolving as the rules evolve (canonicalization is
  mode-independent, never collapses a granularity axis, and expands legacy
  three-segment keys with a trailing `all`). `fps` is `all`
  by default, or the integer-truncated rate when the `per_fps_offsets`
  toggle is on; truncation keeps the NTSC fractional rates distinct from
  their integer siblings (23.976 → `23` vs 24.0 → `24`), which tests pin.
  The audio axis has its own granularity toggle: with
  `distinct_spatial_formats` off, a spatial object-audio variant keys as
  its base codec (`domain/formats.SPATIAL_BASE`: `truehd_atmos` →
  `truehd`, `eac3_ddp_atmos` → `eac3`, `dtshd_ma_x`/`dtshd_ma_x_imax` →
  `dtshd_ma` — the exact variant spellings Kodi's StreamUtils can report,
  observed-only like the HDR aliases; lossy DTS:X over HRA reports as
  plain `dtshd_hra` upstream, so it has no entry). `ch` is `all` by
  default, or the verbatim source channel count when
  `distinct_channel_counts` is on; the count is a source-stream fact
  (Kodi reports the demuxed layout regardless of output config, field-
  verified stable through passthrough flips), and an unusable count
  degrades to `all` identically in lookup and write — channels has no
  completeness gate, unlike fps, so the degradation is a real seam, not
  just a defensive one.
- **Lookup is strict: one candidate key per resolve.** With `per_fps_offsets`
  off the only fps segment consulted is `all`; with it on, the truncated
  rate. The channel axis works the same way under
  `distinct_channel_counts`. With `distinct_spatial_formats` off the audio
  segment is the variant's base codec. There is no fallback between any
  levels. Fps and channel dormancy are symmetric (specific entries sleep
  while their toggle is off, `all` entries while it is on); spatial
  dormancy is one-sided (a base-codec key is legitimate in both modes, so
  only spatial-variant entries sleep, and only while distinct is off). The
  manage view dims and tags whatever is dormant, and flipping any
  toggle is non-destructive both ways. The toggles' help text carries the
  mode contract; there is no flip-time modal, since Kodi only reports a
  settings change on dialog close. `aome/store/resolve.py` and its unit tests
  are the reference for the lookup and write-key rules.
- **A miss does nothing until the addon has acted on the session**, then it
  zero-resets stale residue (silently for the addon's own value, or with an
  "Offset not saved" toast when it discards an unstored manual value).
  `delete`/`clear` leave reset markers that force 0 at the next resolve,
  silently, because the deletion is the authorization.
- **The write key has no history dependence.** The watcher always writes the
  single key derived at store time from the current profile and toggle, never
  conditional on what a lookup hit.
- **Edits take effect immediately.** A settings save or a store mutation is a
  resolve moment: the applier re-runs its decision for the live session, so
  mid-playback edits act at once. A save never wipes the user's hand; only
  the addon's own orphaned residue is reset.
- **`delay_ms` is a verbatim signed integer** at 1 ms resolution, bounded
  only by Kodi's ±10 s. Nothing quantizes it to a step (no 25 ms / 5 ms
  snapping) or clamps it to a narrower range, so custom-build sliders work as
  they are; keep it that way across the store, views, and tests.

## The mutation channel (two processes, one writer)

The management and transfer views run in the script process and never write
the store file. They read it through a read-only reader and ask the service
to mutate over a `JSONRPC.NotifyAll` channel, whitelisted to `delete`,
`clear`, and `import`. There is no `set` op and no value field, so the
channel cannot carry a value write. Acks are matched by request id; no ack
means "service not running", which is reported to the user (there is no
direct-write fallback). `import` is the backup restore: replace-all
semantics, with values only ever coming from a staged backup file
(`offsets.json.import`, the one sibling the script process may write). No
path and no value travel on the wire, and the service re-validates the stage
and refuses an empty one.

## Settings (behavior only)

`settings.xml` holds behavior toggles only: `remember_adjustments` /
`apply_offsets` (an orthogonal learn/apply pair; the watcher never consults
the apply toggle, so apply-off with learn-on is the legal re-teach state),
`per_fps_offsets`, `distinct_spatial_formats` (default on; off collapses
spatial variants onto the base codec's key), `distinct_channel_counts`
(default off; on keys offsets by the source channel count), the
`seek_back_events`
multiselect (its option values are
`SeekScheduler.REASONS` verbatim, pinned by a contract test) with a shared
`seek_back_seconds`, `notify_apply` / `notify_learn` /
`notification_seconds`, `enable_debug_logging`, the action buttons, and a
hidden `coexistence_warned` flag. Working with these:

- The settings object is a live shared proxy, not a snapshot, and it is live
  only while its parent `xbmcaddon.Addon` stays alive. Keep the `Addon` on
  `self`; never read from a throwaway `xbmcaddon.Addon(...).getSettings()`.
- Never write a setting from Python while the settings dialog is open, since
  its save-on-close would clobber the write. Action buttons that navigate
  elsewhere use `<close>true</close>` and let the write settle; service-side
  writes go through the `store_*_if_changed` helpers.
- Offset data is exempt from all of this because it never lives in settings.

## Development workflow

- **Tests gate everything.** Run `python -m pytest tests -q` (a couple of
  seconds) and keep it green before every commit.
- **Deploying to a test box:** script-process-only files (`script_router`,
  `view/*`, the store read path) can be copied over with no Kodi restart;
  service-side changes need a restart to reload.
- **CI** (`ci.yml`) is manual-dispatch only, since the local suite already
  gates commits: `gh workflow run ci.yml --ref main`, then `gh run watch`.

## Packaging constraints

- Only shippable files are tracked without `export-ignore`; dev tooling (this
  file, `.github/`, `tests/`, `tools/`, `.claude/`) is excluded. To check
  what ships: `git archive --format=zip -o /tmp/pkg.zip HEAD` and inspect.
  The package is `aome/` plus the slim XML and resources under the addon id.
- Targets `xbmc.python` 3.0.1 (the Kodi Nexus floor), so use Python 3.8
  syntax throughout.
- `addon.xml` `<news>` is schema-capped at 1500 characters; keep only the
  last couple of versions there and let git history hold older changelogs.

## Releases

Releases are cut by tagging, and the tag gets a GitHub Release. Betas are
published as pre-releases, which do not fire `submit.yml`; publishing a
normal stable Release fires `submit.yml`, which submits the addon to Kodi's
repo-scripts. The version keeps its `~` pre-release suffix until the
deliberate final bump to a clean version. `submit.yml` refuses any `~`
version as a backstop, so bumping `addon.xml` to a release version is a
prerequisite of submission, not a side effect of tagging.

## Conventions

- **Docstrings and comments** follow PEP 257: a one-line summary, then a
  short paragraph or a few bullets covering design intent, behavior, and the
  load-bearing invariants a maintainer must know (concurrency and ordering
  contracts, Kodi quirks, hazards). Aim for tight reference prose, not design
  essays: keep the load-bearing "why" and cut history and repetition. Inline
  comments explain a surprising choice and stay terse.
- **Structure:** constructor dependency injection with required args, no
  globals, no singletons; all runtime code under `resources/lib/aome/`.
- **Kodi I/O** goes through the `kodi/` adapters only. Log through the
  injected sinks with `AOMe_`-prefixed messages (the `e` distinguishes this
  addon's lines from classic AOM's in a shared kodi.log). The debug toggle
  escalates the addon's LOGDEBUG lines to LOGINFO; the Advanced "Export addon
  log" button produces a redacted support report.
- **`strings.po`:** never reuse a retired string id; give new strings a
  translator context comment; contract tests pin id parity and fallback
  defaults.
- **User-facing text** is plain and succinct: describe the behavior and its
  off-state, with no em dashes and no marketing framing.
- **Commits** are small and imperative, with a
  `Co-authored-by: Claude <noreply@anthropic.com>` trailer.
