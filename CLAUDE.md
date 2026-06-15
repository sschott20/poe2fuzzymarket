# CLAUDE.md

PoE2 0.5 ("Runes of Aldur") trade/market tool + a boots craft-and-flip business.
Python package in `src/poe2market/`; launch everything (macro + dashboard + tracker)
with `start_poe2.bat` / `poe2_launcher.py`.

## Read this first
**`POE2_KNOWLEDGE.md`** is the full accumulated reference — game mechanics, the trade-API
gotchas, boots/crafting domain knowledge, pricing lessons, and what every module does.
Read it before doing market/crafting work.

## Non-negotiable rules (these caused real bugs)
- **Offline-only searches:** API has no "offline" status — search `status:"any"` and keep
  listings where `listing.account.online` is falsy. Use `api.is_offline()`.
- **Price filters use the `"divine"` option, never `"exalted"`** (exalted silently misses
  divine-listed items → high brackets look empty).
- **Trust the tracker's clearing price (`track report`) and the user's sold history — NOT the
  offline listing floor** (the floor under-predicts real clearing by ~3–4×).
- Search returns ≤100 hashes (no pagination) → price-walk page. History endpoint = 15 reqs /
  3 hours (fail fast on 429, don't sleep).

## Key commands
- `poe2market track {poll,report,watch}` — the market watcher (validated accurate).
- `/appraise` (skill) or `poe2market appraise --json` — price a pasted boots item.
- `poe2market predictions report` — grade past appraisals vs actual sales.

The boots-business specifics live in the auto-memory (`memory/poe2-boots-crafting-business.md`)
and `research_data/*.json`.
