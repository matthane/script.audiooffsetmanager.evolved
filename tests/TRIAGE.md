# Test-suite triage ledger (Phase E0)

Fate of every test file inherited from `redesign/2.0` (896 tests at branch
cut), per the Evolved design (EVOLVED.md — local-only, git-excluded).
Deletion happens **in the phase that deletes the feature** (E2/E3), never
here; until then dying tests stay present and passing. Reconciled by an
Explore sweep at the end of E2 and E3.

Categories: **KEEP** (survives as-is) · **DIES(phase)** (deleted with its
feature) · **REWRITE(phase)** (replaced in-phase per the key-schema
decision table).

## tests/unit/

| File | Fate | Notes |
|---|---|---|
| test_dispatcher.py | KEEP | Dispatcher core untouched by the store swap |
| test_events.py | KEEP | `StreamProbed` platform fields stay (logging); event set may gain store events in E2 |
| test_gateway.py | KEEP | Gateway surface unchanged |
| test_kodi_log.py | KEEP | Logging rig carries over untouched (E0 item 4) |
| test_session.py | KEEP | Session/state machine untouched |
| test_session_flow.py | KEEP | End-to-end session flows; reciprocal-property name already updated in the identity commit |
| test_seek_policy.py | KEEP | Seek policy unchanged |
| test_seek_scheduler.py | KEEP | Seek scheduler + PM4K coordination unchanged |
| test_delay_parsing.py | KEEP | `parse_delay_ms` — the increment-agnostic pins now carry the §3.1 guarantee |
| test_notifier.py | KEEP | Deferral/dedupe survives; learn-toast wording assertions adjust in E2/E3 detail |
| test_kodi_settings.py | REWRITE(E2) | `OffsetTable` tests re-backed by the store/resolver; behavior-settings coverage (bools/ints, store-if-changed) KEEPS; `is_hdr_enabled`/`fps_override_enabled` per-HDR reads DIE (replaced by global pause + `per_fps_offsets`) |
| test_adjustment_watcher.py | REWRITE(E2) | Store semantics per the D3/D4 table. The `275166f` teardown-phantom pins (liveness gate + 2s quiescence) MUST survive re-plumbing — regression suite re-pointed at the store, never dropped |
| test_offset_applier.py | REWRITE(E2) | Consumes `(entry, hit_kind)`; miss = no-op pins; `new_install` gate and per-HDR enable assertions DIE |
| test_policies.py | REWRITE(E2) | `should_apply` loses `new_install` + `hdr_enabled` gates; `parse_delay_ms` parts KEEP |
| test_runtime.py | REWRITE(E2) | Composition gains the store (path injection, load-at-start) |
| test_stream_detector.py | REWRITE(E2, partial) | Probe/verify orchestration KEEPS; `fps_override_enabled(hdr_type)` per-HDR callable becomes the global `per_fps_offsets` read; fps bucket-whitelist assertions become integer-truncation assertions (open fps axis); platform-write consumers gone but `StreamProbed` posting stays |
| test_stream_profile.py | REWRITE(E1/E2) | `setting_id()` (`hdr_fps_audio` settings-id format) becomes the store key codec (`hdr\|fps\|audio` via `aom/store/keys.py`); `summary()` display coverage KEEPS |
| test_audio_format_matching.py | REWRITE(E1) | `_derive_audio_format` ordered-substring whitelist becomes the open-vocabulary normalization layer; the eac3-before-ac3 ordering lesson carries into corpus-driven tests |
| test_formats.py | REWRITE(E1) | `formats.py` demoted to display names + normalization rules + picker vocabulary; whitelist-role assertions DIE |
| test_platform_recorder.py | DIES(E2) | Component dissolves — platform capability writes are cut (P3) |

## tests/contract/

| File | Fate | Notes |
|---|---|---|
| test_architecture.py | KEEP | Purity contract enforces no-Kodi-imports on the new `aom/store/` too (extend, don't relax) |
| test_settings_matrix.py | DIES(E3) | The 315-id oracle dies with the matrix |
| test_settings_generated.py | DIES(E3) | Generator no-diff test dies with `tools/generate_settings.py` |
| test_strings.py | REWRITE(E3) | Re-pointed at the pruned strings.po + new strings (management view, coexistence warning, learn toast); two-way settings-id contract test added |

## Notes carried into later phases

- **Reciprocal window property renamed** in the identity commit
  (`script.audiooffsetmanagerevolved.seeking`). It was a documented
  courtesy-broadcast protocol under the classic name; consumers (if any)
  see a new sender name. E7 checklist item 11 (PM4K interplay) verifies
  coordination end-to-end — the read side (PM4K's own properties) is
  unchanged.
- **README.md still describes classic AOM** — deliberately untouched until
  the E8 README draft.
- **Classic screenshots/icon/fanart retained as placeholders** — final
  assets are publishing-plan scope (E8 handoff notes).
- `tests/data/observed_formats.txt` is the D11 corpus (Windows harvested
  2026-07-15; CoreELEC pending; grows during E7). It drives the E1
  normalization tests.
