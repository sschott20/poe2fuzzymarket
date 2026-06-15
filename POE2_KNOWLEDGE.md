# PoE2 Trading & Boots-Crafting Knowledge

Accumulated reference for working on this project (a PoE2 0.5 trade/market tool +
boots crafting business). Written 2026-06-15. Flags below: **[verified]** held up
against data/sales; **[inferred]** reasoned but not directly confirmed; numbers
drift with the economy.

> Companion files: per-fact memory in `~/.claude/.../memory/` (MEMORY.md index),
> raw research in `research_data/*.json`, and code in `src/poe2market/`.

---

## 1. Context

- **League:** "Runes of Aldur" (patch 0.5, "Return of the Ancients"), released ~late May 2026.
- **Currency base:** Exalted Orb is the small unit; **Divine** is the high unit.
  1 divine ≈ **160–185 ex** and **drifts daily** — never hard-code it; pull live via
  `stash.fetch_exalted_rates()` (poe2scout). Mirror tier ≈ 2500+ div.
- **0.5 defensive meta [verified]:** **life-based + evasion-dominant.** Pure Energy
  Shield was nerfed hard (recharge cut, instant leech removed). GGG added **Runic Ward**
  (a cheat-death buffer) to push builds back toward life/evasion. Most-played:
  Deadeye (life/evasion), Blood Mage (life). ES/CI is a high-end caster minority.

---

## 2. Trade API gotchas (the reusable, hard-won stuff)

These caused real bugs. Any new search code MUST follow them.

- **OFFLINE-ONLY is policy.** Online listings are dominated by always-online price-fixers
  and mislead. The API has **no "offline" status** — only `online`/`any`. Implement
  offline-only as: search `status:"any"`, then keep listings where
  `listing.account.online` is falsy (the `online` key is **absent** when offline).
  Helper: `api.is_offline(raw)`.
- **Price filters MUST use the `"divine"` option, NOT `"exalted"`.** An exalted-denominated
  price filter **silently fails to match divine-listed items** (which is how all expensive
  items are priced), making high price brackets look empty. This bit us hard ("thin market"
  illusion). `min`/`max` are then interpreted in divine.
- **Search returns ≤100 result hashes, with NO offset/pagination.** To enumerate a larger
  set, **price-walk page:** search from a floor, fetch the cheapest 100, raise the floor to
  the highest price seen, search again, union + de-dup by item hash, until a page returns
  <100. (See `tracker._enumerate_profile`.)
- **Rate limits (per account/IP, header-driven):**
  - search ≈ **5 / 12s**, fetch ≈ **12 / 6s** — short buckets; sleep-and-retry on 429 is fine.
  - **history endpoint ≈ 15 requests per 3 HOURS** (plus 5/60s) — a *separate, brutal* bucket.
    **Fail fast on 429, do NOT sleep** (Retry-After can be minutes → hangs the UI). Frequent
    server restarts each fire an auto-sync and can exhaust this; recovers as requests age out.
  - Use **one process-shared, thread-safe RateLimiter** (the tracker thread + interactive
    requests must cooperate, or they collectively 429). The history endpoint needs its own
    limiter so its tight limits don't pollute the fetch limiter.
- **`category` is `armour.boots`**; ilvl filter via `type_filters.ilvl`; rune sockets via
  `equipment_filters.rune_sockets {min/max}`. Defence type is **NOT** server-filterable —
  tag it post-fetch from the item's Armour/Evasion/EnergyShield **properties**.
- **Bulk exchange** (`/api/trade2/exchange/{league}`) for currency/rune/essence prices; the
  cheap "exalted" offers for big-ticket items (e.g. Ancient Jawbone) are often **bait** —
  read the divine-denominated offers for the real price.
- **PoE rare item names REPEAT** (drawn from a word pool) — they are NOT unique. To match an
  item across data sources, use an **exact-stat fingerprint**, not the name.

---

## 3. Boots domain knowledge

### Defence types (we care about dex/int/dex-int, NOT armour)
- **dex → Evasion (EV)**; **int → Energy Shield (ES)**; **str → Armour (AR)**.
- Base → type: **Boots = EV**, **Sandals/Slippers = ES**, **Shoes = EV+ES hybrid**,
  **Greaves = AR**, **Sabatons = AR/EV**. (Determine from the item's defence *properties*,
  not the base name, for robustness.)
- Focus the business on **EV, ES, EV+ES**; ignore anything with Armour.

### Movement speed
- **35% is the explicit cap** (T1 "Hellion's"). 30% is T2-ish.
- **Pseudo MS = explicit + rune + corruption implicit.** Sources stack:
  - Greater **Rune of Alacrity** = **+5% MS** (the only MS rune; ~3–30ex).
  - A **corruption implicit** "Speed enhancement" = **+5% MS**.
  - So 35 + 5 + 5 = **45% pseudo** is achievable (above-cap).
- **Above-cap MS (40%+) is a real premium but a THIN, illiquid market** — only ~6–8 listed
  league-wide. Strong stat, few buyers → sells slow. Buyers search the *pseudo* MS stat;
  the *explicit* MS filter caps at 35 and won't surface rune/implicit boosts.
- A native 35% explicit is worth more than 30%+rune=35% — track them as different buckets.

### Resistances
- The **pseudo total-resistance** filter **counts rune resists** [verified empirically].
- Value tiers: **res100+ ("triple resist")** is the premium band; res70–99 decent.
- **Fire res is the scarce/demanded element.**
- Chaos resistance is a separate, scarcer, valued suffix.

### Life / mana / other
- **Life is a prefix, valuable, but NOT required** to sell (many boots clear well with no
  life). **+max Mana is a near-dead prefix** — avoid; if exalts keep rolling mana instead of
  life, force life with **Essence of the Body**.
- **Rarity of items found is a suffix** (the recipe staple).
- **Spirit does NOT roll on boots** [verified].
- Stun threshold, attribute, regen, "Deflection Rating from Evasion" = suffixes (mostly filler;
  Deflection-from-Evasion is mildly desirable on EV bases).

### Sockets — the big lever
- **1-socket is the saturated COMMODITY** — even great 1-socket boots (life120, triple-res,
  above-cap MS) floor at ~1d because thousands exist.
- **2-socket + the full package (triple-res + life + rarity)** is the scarce/valuable tier
  (~37d median, ~60 listed). 2-socket alone (e.g. just res70) is also saturated — it's the
  *combination* that's rare.
- 2-socket dex/int bases **with a res roll cost ~10–20d**; blank 2-socket MS35 bases ~1d but
  scarce. With 2 sockets you get **+36% res from two resist runes for free** → an easy path to
  triple-res without paying for a res-rolled base. **Sourcing cheap 2-socket bases is the
  actual bottleneck/edge.**

### Crafting mechanics
- **Essences** (one "crafted"/essence mod allowed per item): **Greater Essence of Seeking =
  rarity** (suffix); **Essence of the Body = life** (prefix). Greater upgrades magic→rare.
- **Ancient Jawbone** (Abyss/Well of Souls): adds a hidden **desecrated** suffix, revealed as a
  choice of 3. **Expensive (~5d, the dominant craft cost).** Take the option that pushes total
  resist into a higher tier (≥70 or ≥100), ideally fire; a big res beats attributes/stun.
- **Omen of Greater Exaltation:** the next Exalt adds **TWO random modifiers**. With suffixes
  full (essence + desecrate + base res), both land in the open **prefixes** → two shots at
  life/ES. This is why a rarity-essence craft still tends to pick up life "for free."
- **Corruption (Vaal Orb):** outcomes include unchanged+tag, a new implicit (e.g. +5% MS,
  Cannot be Frozen), +1 socket, or **rerolling ALL mods into desecrated mods (a brick).**
  Only Vaal cheap failures (≈2.3ex lottery). **Corruption blocks runeforging.** A *good*
  corruption (above-cap-MS implicit, +1 socket) adds real value.
- **Runeforging (Verisium Anvil, Act 1):** adds **Runic Ward** (cheat-death buffer that also
  fuels Kalguuran league gems). ilvl ≤55 = free upside; **ilvl 56+ trades base AR/EV/ES for the
  Ward** (all our bases are 82+, so it's a tradeoff). Additive (keeps mods). **Do it BEFORE any
  corruption** (corruption blocks it). Rising resale premium at the high end on hybrids; not a
  driver on cheap pure-EV.
- **Runes are swappable even on corrupted items** (overwrite by socketing a new one).
- **Iron rune (% inc Armour/Evasion/ES) is almost never the right rune** — it adds nothing
  buyers filter on. Use a **resist rune** (plug the missing element, fire best) or the **+5% MS
  rune** (above-cap on a good boot).

### The standard craft & its economics
Recipe: buy magic base (35% MS + a resist suffix, ideally **2-socket**) → Greater Essence
(Seeking for rarity, or Body for life) → Ancient Jawbone desecrated suffix → Omen of Greater
Exaltation + Perfect Exalted Orb(s) → socket resist runes + quality.
- Rough mats: base (1–20d), essence (~0.1d), **jawbone (~5d)**, omen+perfect exalt (~2–3d),
  runes (~0.1d). Perfect Exalted Orb ≈ 306ex.
- **1-socket craft is a near-guaranteed loss** at current saturation. **2-socket premium
  crafts** (hit life + triple-res) are the positive-EV play — winners-pay-for-losers; track
  your *hit rate* on landing life + res100.

---

## 4. Pricing lessons (validated against real sales 2026-06-15)

**The single most important lesson:**
- **The offline LISTING FLOOR under-predicts actual clearing by ~3–4×.** Cheap offline
  listings are stale/underpriced/unresponsive sellers. A boot whose floor read 1–4d sold for
  **23d**. **Do NOT anchor price estimates to the offline floor.**
- **The tracker's disappearance-based clearing (exit_median) is accurate** — it matched 3 real
  sales within ~10% (9→11d, 23→24d, 51→53d). **Trust it as the primary anchor** when the
  item's exact bucket has data; the user's own SOLD history is the next-best truth.
- A **lone high asking comp** (e.g. one listing at 80d) over-predicts; the item cleared 51d.
- The **calibration factor** (historical sold/exit ratio) was **harmful** (~0.47, halving
  accurate raw exits) because past sales were underpriced/softer — it's now a diagnostic only,
  not applied.
- Appraisal priority order: **tracker bucket exit_median → own comparable sales → (offline
  floor only as a sanity floor, never the estimate).** Don't talk yourself low because "the
  market looks saturated" — the listed floor is not the clearing price.

Snapshot market state (June 2026, drifts): 1-socket EV/ES/EV+ES boots heavily saturated
(1d floors but ~9–25d real clears for decent rolls); 2-socket triple-res+life ~37–80d; premium
ES caster 2-socket (e.g. life + triple-res + high ES%) ~50–80d.

---

## 5. Tooling in this repo (what exists, how to run)

- **`poe2market` CLI** (`src/poe2market/cli.py`): `config`, `stats`, `categories`, `deals`,
  `analyze`, `history`, `serve`, `track`, `appraise`, `predictions`.
- **Sale tracker / "market watcher"** (`tracker.py`): polls the offline boots market every
  ~13 min (config `tracker_minutes`), price-walk-enumerates tight **product buckets**, and
  infers **clearing prices** from listings disappearing. **VALIDATED accurate.** Buckets focus
  on EV/ES/EV+ES, 2-socket weighted, capped at `MAX_TRACK_DIV=400`. `poe2market track
  {poll,report,watch}`. Data in `~/.cache/poe2market/tracker.db`.
- **/appraise skill** (`.claude/skills/appraise/SKILL.md` + `appraise.py`): paste an in-game
  boots item → model parses it to a spec → `poe2market appraise --json` gathers comparable
  sales + tracker clearing + offline listings → model gives **price + best rune + craft/
  desecration advice.** Boots only.
- **Prediction calibration** (`predictions.py`): `/appraise` records each guess with the full
  spec; `poe2market predictions report` grades predicted-vs-actual, matched by **exact stat
  fingerprint** (name is only a fallback — names repeat) and **only against sales after the
  prediction** (forward-looking).
- **Web dashboard** (`web.py`): tabs for Deals / Analyze / Sale History / **Tracker** /
  Settings; runs the tracker + history auto-sync as startup daemon threads. The Tracker tab
  shows clearing prices, stale ceilings, and a filterable all-items table.
- **Launcher:** `start_poe2.bat` → `poe2_launcher.py` starts the **macro overlay + dashboard
  + tracker + auto-sync together**. (`poe2market serve` alone runs the dashboard+tracker but
  NOT the macro.) `poe2_macro.py` is the in-game overlay (Shift+4 toggle, Shift+5 quit).
- **Data stores** (`~/.cache/poe2market/`): `history.db` (your hideout sales — the ground
  truth), `tracker.db` (lifecycle/clearing), `observations.db`, `cache.db`, `predictions.db`.
  Ad-hoc trade searches do NOT auto-populate observations.db.

### mod_sig bucket format (used by tracker & appraiser)
`defc | <n>s | ms<tier>[r] | res<bucket> | life<bucket> | rar<Y/N> [|ch8+/ch15+] [|corr] [|rf]`
- ms tier: `<30 / 30-34 / 35 / 36+`; trailing **`r`** = MS is rune-boosted (vs native).
- res bucket: `0-39 / 40-69 / 70-99 / 100+` (total ele res incl. runes).
- life bucket: `0 / 1-79 / 80-119 / 120+`. rar: Y if ≥10%. ch8+/ch15+ = chaos res. corr =
  corrupted. rf = runeforged.
- Example: `EV|2s|ms35|res100+|life80-119|rarY` (a premium 2-socket triple-res life boot).

---

## 6. Gotchas / known noise to remember

- **`track report` "cut%"/reprice counts are inflated** by divine-rate drift: prices are stored
  in exalted, so when the divine rate moves between polls, a listing's converted value changes
  and looks like a reprice. Doesn't affect clearing accuracy (converted back). Ignore raw cut%.
- After a schema/FOCUS change, expect a one-time spike in tracker "gone" counts (old buckets
  + armour items aging out) — **not** real sales.
- The tracker runs **inside** `serve`; stopping the web server pauses the watcher (data
  persists). A shared rate limiter means manual CLI commands no longer need the server killed.
- History sync can be temporarily locked out (15/3hr limit) after many restarts — it self-heals.
