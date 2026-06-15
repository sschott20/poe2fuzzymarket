---
name: appraise
description: Appraise a pasted PoE2 boots item — price estimate, best rune to socket, and crafting/desecration advice — using the user's past sales, the live tracker, and fresh offline trade searches. Use when the user pastes an in-game item (Ctrl-C text) and wants a price and/or what to do with it.
---

# Appraise a boots item

The user pastes an in-game item (PoE2 Ctrl-C text). Parse it yourself, call the
evidence engine, then give a **price estimate + rune pick + craft advice**.

Boots only, and only the defence types the user crafts: **pure int (ES), pure dex
(EV), and dex-int (EV+ES)**. If the item has any **Armour**, or isn't boots, say so
and stop — they don't care about it.

Read the project memory `poe2-boots-crafting-business` and `offline-only-search-default`
first; the recommendations below come from that research.

## Step 1 — Parse the pasted item into a spec

Extract these fields (you are reliable at reading item text; be tolerant of format):

- `base` — the base type line (e.g. "Cinched Boots").
- `defc` — defence type from the **property lines present**: Armour→AR, Evasion Rating→EV,
  Energy Shield→ES. Combine in that order, e.g. EV+ES, AR+ES. (Base name corroborates:
  Boots=EV, Sandals/Slippers=ES, Shoes=EV+ES hybrid, Greaves=AR, Sabatons=AR/EV.)
- `sockets` — number of rune sockets (socketed runes + empty). If unclear from the text,
  estimate and note the uncertainty.
- `ilvl` — Item Level.
- `corrupted` — true if a "Corrupted" line is present.
- `rarity_kind` — rare / magic / normal.
- `ms` — `% increased Movement Speed` from explicit mods **plus** any `(rune)` MS line
  (the +5% Rune of Alacrity). This matches how buyers search (pseudo MS).
- `ms_explicit` — the `% increased Movement Speed` from the **explicit** mod only (exclude
  the rune). A native 35% is worth more than 30%+rune=35%, so report both. Set `ms_rune`
  true if a `(rune)` MS line is present.
- `chaos_res` — `+#% to Chaos Resistance` (a scarcer, valued suffix; include it).
- `life` — `+# to maximum Life`.
- `res` — **sum** of all `+#% to Fire/Cold/Lightning Resistance` across explicit, crafted,
  desecrated **and rune** lines (matches the trade pseudo-total-resistance buyers filter on).
- `chaos_res` — `+#% to Chaos Resistance`.
- `rarity` — `#% increased Rarity of Items found`.
- `open_prefix`, `open_suffix` — classify each **affix** mod (explicit/crafted/desecrated,
  NOT rune/implicit) as prefix or suffix, count them, and set open = 3 − used (rares are
  3+3; magic is 1+1). Classification for boots:
  - **Prefix:** maximum Life · increased/flat Energy Shield · increased/flat Evasion ·
    increased/flat Armour · increased Movement Speed · maximum Mana · Spirit · hybrid
    "Evasion and Energy Shield" / "Armour and Evasion".
  - **Suffix:** Fire/Cold/Lightning/Chaos/all-Elemental Resistance · increased Rarity of
    Items found · Strength/Dexterity/Intelligence · Stun Threshold · Life/Mana Regeneration ·
    reduced ailment duration · Deflection Rating from Evasion · Cooldown Recovery.

Build a JSON object with exactly those keys.

## Step 2 — Run the evidence engine

```
poe2market appraise --json '<the spec JSON>'
```

It returns: `comparable_sales` (the user's own sold boots — ground truth — split into
exact / similar / same_defence), `tracker` (clearing-price bucket if known + active
comparable listings), and `live_offline` (fresh offline listings matching the key mods,
with floor/p25/median/p75 in divine). All prices in divine.

## Step 3 — Synthesize

**Price estimate** — give a range with confidence, in this priority order:
1. **`tracker.bucket` exit_median is the PRIMARY anchor when present.** Validated 2026-06-15:
   it matched 3 actual sales within ~10% (9→11d, 23→24d, 51→53d). It's disappearance-based
   real clearing data — if the item's exact bucket has tracker exits, lead with that number.
2. **The user's own `comparable_sales`** (exact > similar) — corroborate / fill in when the
   tracker bucket is thin or empty.
3. **`live_offline` is the WEAKEST signal — do NOT anchor to it.** It's a cheapest-first
   offline pool dominated by underpriced/stale listings, and it has empirically
   UNDER-predicted actual clearing by 3–4× (a boot whose floor read ~1–4d sold for 23d).
   Use it only as a sanity floor, never as the estimate.
- Hard lesson from 2026-06-15: appraisals anchored on the offline floor ran badly LOW on
  commodity/mid boots (−72%). Trust the tracker's clearing and the user's real sales over
  what's currently listed. Don't talk yourself into a low number because "the market looks
  saturated" — the listed floor is NOT the clearing price. State the basis (n of each) and
  your confidence; don't over-precision a thin sample.

**Best rune** (research-backed):
- Finished, good boot (35% MS + decent life/res) with a socket to spare → **Greater Rune of
  Alacrity (+5% MS)**: pushes to 40% pseudo = above-cap, a large premium (but a thin market).
- Otherwise → a **resist rune** plugging the item's weakest/missing element (fire is the
  scarce, demanded one), to reach a res tier (≥70, ≥100). **Never the Iron rune** — it adds
  nothing buyers filter on. Runes are swappable even on corrupted items.

**Craft advice** (only if `rarity_kind` ≠ rare-and-full, i.e. in-progress):
- Magic base → essence: default **Greater Essence of Seeking** (rarity, a suffix); use a
  **resist essence** only if the base's native res is weak. Keep rarity — the double-exalt
  drags life along for free.
- Suffixes full + prefixes open → **Omen of Greater Exaltation + Perfect Exalt** (two random
  mods land in the open prefixes → shots at life/ES).
- **Desecration choice** (Ancient Jawbone reveals a suffix): take the option that pushes
  **total resistance into a higher bucket** (≥70 or ≥100 drive value), ideally fire; a big
  res roll beats attributes/stun/regen. On EV bases, "Deflection Rating from Evasion" is also
  desirable. Avoid bricking a good prefix when suffixes are full.
- **Corrupt?** Only worth it on 1–2d failures (a 2.3ex lottery). Never on a finished winner —
  it bricks ~60% of the time and **blocks runeforging**.
- **Runeforge?** Optional; Runic Ward demand rises at the high end (esp. EV+ES hybrids).
  Trades base defence on ilvl 56+ bases. If doing it, do it **before** any corruption.

Keep the answer tight: lead with the price range, then the one rune to socket, then the
single best next craft step. Show the comps you leaned on.

## Step 4 — record the prediction (so it can be graded later)

After giving the price, save your guess keyed by the item's unique rare NAME (the line
right after "Rarity: Rare", e.g. "Corpse League"). When that item later shows up in the
synced sale history, `poe2market predictions report` grades predicted-vs-actual.

Pass the SAME spec JSON you used for `appraise` (stores exact stats, so it can be matched
later by stat fingerprint even if the name is missing) plus the name and your low/high:

```
poe2market predictions add --spec '<the same spec JSON>' --name "<rare name>" \
  --low <low_div> --high <high_div> --note "<one-line reasoning>"
```

Use the same low/high divine numbers you told the user. Always record finished-item
appraisals; skip only when the user is mid-craft rather than pricing a finished item.
