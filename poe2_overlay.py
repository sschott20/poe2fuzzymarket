#!/usr/bin/env python3
"""PoE2 Acts quick-guide overlay.

Usage:
    python poe2_overlay.py            # resume at last step
    python poe2_overlay.py 42         # start at step 42 (1-indexed)

Global hotkeys (work while PoE2 is focused, Windows only):
    Down arrow   next step
    Up arrow     previous step

When the overlay itself is focused:
    Esc          save and quit
    M            toggle drag mode
    [ / ]        shrink / enlarge font

The 'Move' button on the overlay also toggles drag mode. When drag mode is
on, the border lights up orange and you can click-drag anywhere on the
window to move it. Click 'Lock' to re-lock.

State (current step + window position + font size) is saved to
    ~/.poe2_acts_guide_state.json
"""
import ctypes
import json
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

STATE_FILE = Path.home() / ".poe2_acts_guide_state.json"

GUIDE = r"""
## Act 1
- Kill Bloated Miller - Use skillpoint - Go to Clearfell Encampment.
- Town (Clearfell Encampment) - Talk to NPCs - Go to Clearfell.
- Clearfell - Kill Beira of the Rotten Pack (Cold Resistance bonus) - Go to Grelwood.
- Grelwood - Tag WP - Summon Una (near WP) - Go to Red Vale (opposite side).
- Red Vale - Activate 3 Obelisks of Rust & Kill The Rust King - Take Runed GirdleRuned GirdleThe rune on the buckle means 'unity' or 'safety from harm.', Runed GuardRuned GuardThe rune at the base of the blade means 'to strike true' or 'truth.' and Runed Skull CapRuned Skull CapThe rune emblazoned on the crest means 'freedom' or 'for a worthy cause.' - Portal to Town.
- Town - Talk to NPCs for Runed SpikesRuned SpikesTogether, the runes are an oath of peaceful passage and a request for freedom in pursuit of an important cause. - WP to Grelwood.
- Grelwood - Activate 3 Runic Seals - Portal to Town.
- Town - Talk to NPCs - Go to Grelwood.
- Grelwood - Go to Grim Tangle (center of zone) - Summon Una - Go to Cemetery of the Eternals.
- Cemetery - Talk to Lachlann - Enter Mausoleum of the Praetor - Kill Draven, the Eternal Praetor - Take Draven's Memorial Key PieceDraven's Memorial Key Piece... shall never be forgotten. - Go to Cemetery.
- Cemetery - Enter Tomb of the Consort (opposite WP) - Kill Asinia, the Praetor's Consort - Take Asinia's Memorial Key PieceAsinia's Memorial Key PieceThe cruelty of the Eternals... - Go to Cemetery.
- Cemetery - Talk to Lachlann - Kill Lachlann of Endless Lament - Take Count Lachlann's RingCount Lachlann's RingThe rune of Authority is said to dim one lunar cycle after a Count's soul is laid to rest. - Portal to Town.
- Town - Talk to NPCs - WP to Hunting Grounds.
- Hunting Grounds - Talk to Delwyn - Kill The Crowbell (2 skillpoints) - Go to Freythorn.
- Freythorn - Complete all rituals - Kill The King in the Mists (30 Spirit bonus) - Talk to Finn - Portal to Town.
- Town - Talk to NPCs - WP to Hunting Grounds - Go to Ogham Farmlands.
- Ogham Farmlands - Tag WP - Take Una's LuteUna's LuteThis grelwood heirloom hums softly with latent emotion, imbued by centuries of songs passed from mother to daughter. from Una's Lute Box (before farms) - Go to Ogham Village.
- Once per league: Ogham Village - Tag WP - Take Smithing ToolsSmithing ToolsForgework tools belong to Renly and his son. (salvage bench) - Portal to Town - Talk to Renly - Return to Ogham Village.
- Ogham Village - Kill The Executioner (end of zone) - Use Lever - Talk to Leitis - Go to Manor Ramparts.
- Manor Ramparts - Tag WP - Go to Town - Talk to NPCs (2 skillpoints) - WP to Manor Ramparts - Go to Ogham Manor.
- Ogham Manor - Kill Candlemass, the Living Rite (Life bonus) - Go down stairs to Arena - Kill Count Geonor - Portal to Town.
- Town - Talk to NPCs - Talk to Hooded One - Go to Act 2.

## Act 2
- Vastiri Outskirts - Kill Rathbreaker - Portal to Town - Talk to Zarka - Go to Ardura Caravan.
- Town (Ardura Caravan) - Talk to NPCs - Desert Map to Mawdun Quarry.
- Mawdun Quarry - Go to Mawdun Mine.
- Mawdun Mine - Tag WP - Kill Rudja, the Dread Engineer - Open Cage - Talk to Risu - Portal to Town.
- Town - Talk to NPCs - Desert Map to Halani Gates.
- Halani Gates - Talk to Asala - Go to Town.
- Town - Talk to NPCs - Desert Map to Traitor's Passage.
- Traitor's Passage - Tag WP - Activate Ancient Seal (middle of zone) - Activate 3 Runic Seals - Kill Balbala, the Traitor - Take Balbala's BaryaBalbala's BaryaArea Level 22, Number of Trials 1, take this item to the Trial of the Sekhemas. - Go to Halani Gates.
- Halani Gates - Tag WP - Summon Asala - Defeat Jamanra, the Risen King - Go to sandstorm - Portal to Town.
- Town - Talk to NPCs - Desert Map to Keth.
- Keth - Kill serpents for Kabala Clan RelicKabala Clan RelicBy the terms of the Third Pact, the Constrictor Queen was banished from Keth. - Kill Kabala, Constrictor Queen (2 skillpoints) - Go to Lost City.
- Lost City - Go to Buried Shrines - Go to Heart of Keth.
- Heart of Keth - Kill Azarian, the Forsaken Son - Talk to Water Goddess - Take Everburning Cinders - Ignite Water Goddess - Take The Essence of WaterThe Essence of WaterThe last few drops from veins dry as sand. - Portal to Town.
- Town - Talk to NPCs - Desert Map to Mastodon Badlands.
- Mastodon Badlands - Go to Lightless Passage (center of zone) - Go to Well of Souls.
- Well of Souls - Speak to Lurking Creature (Reveal Desecrated mods) - Speak to Sin - WP to Lightless Passage - Return to Mastodon Badlands.
- Mastodon Badlands - Go to Bone Pits.
- Bone Pits - Tag WP - Kill monsters for Sun Clan RelicSun Clan RelicBy the terms of the Third Pact, the Sun Clan agreed to never again attack the Maraketh. - Go to Blackrib Pit - Kill Iktab, the Deathlord and Ekbab, Ancient Steed - Take Mastodon TusksMastodon TusksAge-old ivory from a forgotten era. - Portal to Town.
- Town - Talk to NPCs - Desert Map to Valley of Titans.
- Valley of Titans - Tag WP - Activate Medallion near WP (Charm bonus, can be changed) - Activate 3 Ancient Seals around the zone - Enter Titan Grotto.
- Titan Grotto - Kill Zalmarath, the Colossus - Take The Flame RubyThe Flame RubyIt burns with the primordial heat of a long-ago Wraeclast. - Portal to Town.
- Town - Talk to NPCs for The Horn of the VastiriThe Horn of the VastiriTravel to the Sandstorm and sound this Horn to open the way. - Desert Map to Traitor's Passage - Go to the front of the caravan and Sound The Horn - Talk to NPCs - Desert Map to Deshar.
- Deshar - Find Lailuma's Body (Fallen Dekhara) - Take Final LetterFinal LetterExtremely personal words of love, honour, and hopelessness. - Kill Hunin, Storm Caller and Mugin, Frost Bringer - Take Djinn BaryaDjinn BaryaTake this item to the Relic Altar at the Trial of the Sekhemas. - Go to Path of Mourning.
- Path of Mourning - Go to Spires of Deshar.
- Spires of Deshar - Tag WP - Activate Sisters of Garukhan shrine (Lightning Resistance boost) - Kill Tor Gul, the Defiler - Portal to Town.
- Town - Talk to NPCs (2 skillpoints) - Desert Map to Dreadnought.
- Dreadnought - Go to Dreadnought Vanguard - Tag WP - Kill Jamanra, the Abomination - Portal to Town.
- Town - Talk to NPCs - Talk to Asala - Go to Act 3.

## Act 3
- Sandswept Marsh - Go to Ziggurat Encampment.
- Town (Ziggurat Encampment) - Talk to NPCs - Go to Jungle Ruins (top).
- Jungle Ruins - Kill Mighty Silverfist (2 skillpoints) - Go to Venom Crypts (center of zone near WP).
- Venom Crypts - Take Corpse-snake VenomCorpse-snake VenomBeware the bite of those serpents that call a human corpse home. from Corpse (end of zone) - Portal to Town.
- Town - Talk to Servi (Various boosts, cannot be changed) - WP to Jungle Ruins - Go to Infested Barrens (opposite from WP).
- Infested Barrens - Tag WP - Summon Alva - Go to Azak Bog.
- Azag Bog - Tag WP - Summon Servi - Kill Ignagduk, the Bog Witch (30 Spirit bonus) - Take Ignagduk's Ghastly SpearIgnagduk's Ghastly SpearThe skulls of stolen children adorn carved wood. - Portal to Town.
- Town - Talk to NPCs - WP to Infested Barrens - Go to Chimeral Wetlands (opposite Azak Bog).
- Chimeral Wetlands - Tag WP - Kill Xyclucian, the Chimera - Take Chimeral Inscribed UltimatumChimeral Inscribed UltimatumTake this item to The Temple of Chaos to participate in a Trial of Chaos. - Go to Jiquani's Machinarium (within boss arena).
- Jiquani's Machinarium - Tag WP - Summon Alva - Take Small Soul CoreSmall Soul CoreAncient facets remain warm to the touch. - Activate Stone Altar - Take more Small Soul Cores & Activate Stone Altars - Kill Blackjaw, the Remnant (Fire Resistance boost) - Go to Jiquani's Sanctum.
- Jiquani's Sanctum - Tag WP - Summon Alva - Find 3 Medium Soul Cores & Start 2 Generators (corners of zone) - Activate Large Soul Core near Alva - Kill Zicoatl, Warden of the Core - Take Large Soul CoreLarge Soul CoreHeat emanates from the facets. - WP to Infested Barrens.
- Infested Barrens - Activate Stone Altar (near WP) - Go to Matlan Waterways (nearby).
- Matlan Waterways - Pull all the Canal Levers - Pull the large Canal Lever (end of zone) - Portal to Town.
- Town - Go down the stairs - Talk to Alva - Go to Drowned City.
- Drowned City - Tag WP - Summon Oswald - Go to Molten Vault.
- Once per league: Molten Vault - Tag WP - Use Lever - Pull Sluice Gate Lever - Kill Mektul, the Forgemaster - Take The Hammer of KamasaThe Hammer of KamasaThe smith may not remember higher pursuits, but the hammer does. - Talk to Oswald (reforging bench) - Portal to Town.
- Town - Talk to NPCs - WP to Drowned City - Go to Apex of Filth.
- Apex of Filth - Tag WP - Kill Queen of Filth (end of zone) - Take Temple Door IdolTemple Door IdolThis idol seems strangely shaped. - Portal to Town.
- Town - Go down the stairs - Talk to Alva - Open Door - Go to Temple of Kopec.
- Temple of Kopec - Climb Stairs twice (corner of pyramid) - Kill Ketzuli, High Priest of the Sun - Summon Alva, Investigate Platform - Enter gateway - Tag WP (behind gateway) - Go down stairs - Go to Utzaal.
- Utzaal - Tag WP - Kill Viper Napuatzi - Kill monsters for Sacrificial HeartSacrificial HeartA soul still clings to fading shreds of life. - Go to Aggorat.
- Aggorat - Tag WP - Go to altar - Take Sacrificial DaggerSacrificial DaggerNecessary to ritually sacrifice a Heart at the proper site. - Place and stab Sacrificial Heart (2 skillpoints) - Go to Black Chambers.
- Black Chambers - Tag WP - Kill Doryani, Royal Thaumaturge and Doryani's Triumph - Talk to Doryani - Portal to Town - Talk to Doryani for apocalypse.
- Town - Talk to NPCs - Talk to Alva - Go to Act 4.

## Act 4
- Town (Kingsmarch) - Talk to NPCs for Book Charter - Talk to Makoru - Sail to Kedge Bay.
- Kedge Bay - Go to Journey's End.
- Journey's End - Summon Tujen - Kill Captain Hartlin (end of zone) - Take VerisiumVerisiumA shooting star may bring great fortune. - Portal to Town.
- Town - Talk to Dannig for Verisium SpikesVerisium SpikesEngraved with the runes for 'Authority,' 'Break,' and 'Release.' - WP to Journey's End.
- Journey's End - Talk to Freya - Activate Karui Totems - Kill Omniphobia, Fear Manifest - Portal to Town.
- Town - Talk to Tujen (2 skillpoints) - Talk to Makoru - Sail to Isle of Kin.
- Isle of Kin - Kill The Blind Beast (optional) - Go to Volcanic Warrens.
- Volcanic Warrens - Kill Krutog, Lord of Kin (end of zone) - Talk to Hooded One - Return to Ship.
- Ship - Talk to Makoru - Sail to Abandoned Prison.
- Abandoned Prison - Kill monsters for Chapel KeyChapel KeyUnlocks the Chapel Door in the Abandoned Prison. - Open Chapel Door - Tag WP - Activate Goddess of Justice (Flask recovery, can be changed) - Use Levers - Go to Solitary Confinement.
- Solitary Confinement - Use Levers - Kill The Prisoner - Talk to Hooded One - Return to Ship.
- Ship - Talk to Makoru - Sail to Whakapanu Island.
- Whakapanu Island - Go to Singing Caverns.
- Singing Caverns - Tag WP - Kill Diamora, Song of Death - Talk to Hooded One - Return to Ship.
- Ship - Talk to Makoru - Sail to Shrike Island.
- Shrike Island - Kill Scourge of the Skies (end of zone) - Talk to Hooded One - Return to Ship.
- Ship - Talk to Makoru - Sail to Eye of Hinekora.
- Eye of Hinekora - Talk to NPCs - Activate Well of Hinekora - Pass 3 tests - Click Pay your Respects (Mana boost) - Go to Halls of the Dead (further down).
- Halls of the Dead - Tag WP - Pass Tawhoa's test (Dex/Lightning Resistance) - Pass Tasalio's test (Int/Cold Resistance) - Pass Ngamahu's test (Str/Fire Resistance) - Defeat Yama The White - Take Silver CoinSilver CoinExchange with Navali in Halls of the Dead. - Go to Trial of the Ancestors.
- Trial of the Ancestors - Tag WP - Talk to Navali - Take Tattoo of HinekoraTattoo of HinekoraGrants two Weapon Set Passive Skill Points. (2 skillpoints) - Return to Ship.
- Ship - Talk to Makoru - Sail to Arastas.
- Arastas - Tag WP - Talk to Missionary Lorandis - Enter church, Exit the church, Destroy forcefield - Kill Torvian, Hand of the Saviour - Go to Excavation.
- Excavation - Kill Benedictus, First Herald of Utopia - Enter forge - Talk to Hooded One - Portal to Town.
- Town - Talk to NPCs - Talk to Rhodri (at the ship) - Sail to Ngakanu.
- Ngakanu - Go to Heart of the Tribe.
- Heart of the Tribe - Tag WP - Kill Tavakai, the Chieftain, Tavakai, the Fallen and Tavakai, the Consumed - Portal to Town.
- Town - Talk to NPCs - Talk to Hooded One - Go to Ogham, Vastiri, or Mount Kriar.

## Interlude: Ogham
- Town (The Refuge) - Talk to NPCs - Go to Scorched Farmlands (bottom exit).
- Scorched Farmlands - Kill Heldra of the Black Pyre and Isolde of the White Shroud - Go to Stones of Serle.
- Stones of Serle - Activate all Runed Megaliths - Go to center rune - Kill Siora, Blade of the Mists - Talk to Una - Go to Scorched Farmlands.
- Scorched Farmlands - Go to darkness - Go to Blackwood - Go to Holten.
- Holten - Go to Wolvenhold.
- Wolvenhold - Kill Oswin, the Dread Warden (2 skillpoints) - Go to Holten - Go to Holten Estate.
- Holten Estate - Tag WP - Go upstairs, then downstairs - Kill Thane Wulfric and Lady Elswyth (ground floor) - Portal to Town.
- Town - Talk to NPCs - Talk to Hooded One - Go to Vastiri.

## Interlude: Vastiri
- Town (The Khari Bazaar) - Talk to NPCs - Go to Khari Crossing.
- Khari Crossing - Kill Akthi, the Final Sting and Anundr, the Sandworm (top right) - Portal to Town.
- Town - Talk to Risu (2 skillpoints) - Return to Khari Crossing.
- Khari Crossing - Go to Skullmaw Stairway (top left) - Take Molten One's GiftMolten One's GiftGrants 5% increased Maximum Life. (Life boost) - Return to Khari Crossing - Go to Pools of Khatal (bottom left).
- Pools of Khatal - Tag WP - Go to Sel Khari Sanctuary.
- Sel Khari Sanctuary - Tag WP - Kill Elzarah, the Cobra Lord - Talk to Asala - Portal to Town.
- Town - Talk to NPCs - Go to Khari Crossing - Go to Galai Gates (left).
- Galai Gates - Tag WP - Kill Vornas, the Fell Flame - Go to Qimah.
- Qimah - Tag WP - Activate Seven Pillars (Various boosts, can be changed) - Summon Jado (end of zone) - Go to Qimah Reservoir.
- Qimah Reservoir - Tag WP - Kill Azmadi, the Faridun Prince - Activate Grand Barya - Talk to Jado - Portal to Town.
- Town - Talk to NPCs - Talk to Hooded One - Go to Mount Kriar.

## Interlude: Mount Kriar
- Town (The Glade) - Talk to NPCs - Go to Ashen Forest.
- Ashen Forest - Go to Kriar Village.
- Kriar Village - Tag WP - Kill Lythara, the Wayward Spear (40 Spirit boost) - Go to Glacial Tarn.
- Glacial Tarn - Tag WP - Go to Howling Caves.
- Howling Caves - Tag WP - Kill The Abominable Yeti - Take Icy TusksIcy TusksFrozen trophies of a predator's reign. - Portal to Town.
- Town - Talk to Hilda (2 skillpoints) - WP to Glacial Tarn.
- Glacial Tarn - Kill Rakkar, the Frozen Talon - Go to Kriar Peaks.
- Kriar Peaks - Tag WP - Take gift from Elder Madox - Go to Etched Ravine.
- Etched Ravine - Tag WP - Kill Stormgore, the Guardian - Go to Cuachic Vault.
- Cuachic Vault - Tag WP - Kill Zelina, Blood Priestess and Zolin, Blood Priest - Summon Doryani - Portal to Town.

## Endgame
- WP to Kingsmarch (Act 4 town).
- Kingsmarch - Talk to NPCs - Talk to Hooded One (2 skillpoints) - Travel to Oriath.
- The Ziggurat Refuge - Talk to NPCs - Run one map in Ziggurat to unlock maps - Start mapping in hideout.
"""


def parse_steps(raw):
    steps = []
    act = ""
    tooltip_re = re.compile(
        r"([A-Z][\w' ]{2,}?)\1.*?(?=, [A-Z]| and [A-Z]| - | \(|$)"
    )
    for ln in raw.splitlines():
        ln = ln.rstrip()
        if ln.startswith("## "):
            act = ln[3:].strip()
        elif ln.startswith("- ") and act:
            text = ln[2:].strip()
            text = tooltip_re.sub(r"\1", text)
            text = re.sub(r"\s+", " ", text).strip()
            steps.append((act, text))
    return steps


STEPS = parse_steps(GUIDE)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(d):
    try:
        STATE_FILE.write_text(json.dumps(d))
    except Exception:
        pass


saved = load_state()

if len(sys.argv) > 1:
    try:
        start = max(0, min(int(sys.argv[1]) - 1, len(STEPS) - 1))
    except ValueError:
        print(f"Invalid step number. Use 1..{len(STEPS)}", file=sys.stderr)
        start = saved.get("index", 0)
else:
    start = saved.get("index", 0)
idx = max(0, min(start, len(STEPS) - 1))

TRANSP = "#010101"
BG = TRANSP
DRAG_BG = "#1a1a1a"
BORDER = TRANSP
BORDER_ON = "#e8a500"
FG_CUR = "#ffffff"
FG_NEXT = "#94b8ff"
FG_DIM = "#777"

root = tk.Tk()
root.title("PoE2 Acts")
root.overrideredirect(True)
root.attributes("-topmost", True)
alpha = saved.get("alpha", 1.0)
root.attributes("-alpha", alpha)
root.attributes("-transparentcolor", TRANSP)
root.configure(bg=BG)

ww, hh = saved.get("size", [300, 100])
xx, yy = saved.get("pos", [120, 120])
root.geometry(f"{ww}x{hh}+{xx}+{yy}")
font_sz = saved.get("font", 9)

frame = tk.Frame(
    root, bg=BG, bd=0, highlightthickness=2,
    highlightbackground=BORDER, highlightcolor=BORDER,
)
frame.pack(fill="both", expand=True)

body = tk.Frame(frame, bg=BG)
body.pack(fill="both", expand=True, padx=6, pady=2)

cur_lbl = tk.Label(
    body, text="", bg=BG, fg=FG_CUR,
    font=("Segoe UI", font_sz, "bold"),
    anchor="nw", justify="left", wraplength=ww - 20,
)
cur_lbl.pack(fill="both", expand=True, pady=(2, 2))

nxt_lbl = tk.Label(
    body, text="", bg=BG, fg=FG_NEXT,
    font=("Segoe UI", max(8, font_sz - 2)),
    anchor="nw", justify="left", wraplength=ww - 20,
)
nxt_lbl.pack(fill="x", pady=(0, 2))


def _btn(parent, text, cmd):
    return tk.Button(
        parent, text=text, command=cmd, bg="#1d1d1d", fg="#dddddd",
        activebackground="#333333", activeforeground="#ffffff",
        bd=0, padx=6, pady=0, font=("Segoe UI", 8),
        highlightthickness=0, cursor="hand2",
    )


btn_bar = tk.Frame(frame, bg=BG)

foot = tk.Label(
    frame, text="↑/↓ step   M drag   [ ] font   , . alpha   Esc quit",
    bg=BG, fg=FG_DIM, font=("Segoe UI", 7), anchor="w",
)

grip = tk.Label(frame, text="\u25E2", bg=BG, fg="#555",
                font=("Segoe UI", 9), cursor="bottom_right_corner")

drag_mode = False
hovering = False


def render():
    _, txt = STEPS[idx]
    cur_lbl.config(text=txt)
    if idx + 1 < len(STEPS):
        _, nt = STEPS[idx + 1]
        nxt_lbl.config(text=nt)
    else:
        nxt_lbl.config(text="— end of guide —")


def persist():
    try:
        save_state({
            "index":  idx,
            "pos":   [root.winfo_x(), root.winfo_y()],
            "size":  [root.winfo_width(), root.winfo_height()],
            "font":  font_sz,
            "alpha": alpha,
        })
    except tk.TclError:
        pass


def step_next(*_):
    global idx
    if idx < len(STEPS) - 1:
        idx += 1
        render()
        persist()


def step_prev(*_):
    global idx
    if idx > 0:
        idx -= 1
        render()
        persist()


def quit_app(*_):
    persist()
    root.destroy()


def _set_bg(color):
    for w in (root, frame, body, btn_bar, cur_lbl, nxt_lbl, foot, grip):
        try:
            w.configure(bg=color)
        except tk.TclError:
            pass


def toggle_drag(*_):
    global drag_mode
    drag_mode = not drag_mode
    if drag_mode:
        _set_bg(DRAG_BG)
        frame.config(highlightbackground=BORDER_ON)
    else:
        _set_bg(BG)
        frame.config(highlightbackground=BORDER)
    try:
        drag_btn.config(text="Lock" if drag_mode else "Move")
    except tk.TclError:
        pass


def grow(*_):
    global font_sz
    font_sz = min(20, font_sz + 1)
    cur_lbl.config(font=("Segoe UI", font_sz, "bold"))
    nxt_lbl.config(font=("Segoe UI", max(8, font_sz - 2)))
    persist()


def shrink(*_):
    global font_sz
    font_sz = max(8, font_sz - 1)
    cur_lbl.config(font=("Segoe UI", font_sz, "bold"))
    nxt_lbl.config(font=("Segoe UI", max(8, font_sz - 2)))
    persist()


drag_btn = _btn(btn_bar, "Move", toggle_drag)
drag_btn.pack(side="right", padx=(2, 0))
_btn(btn_bar, "\u00d7", quit_app).pack(side="right", padx=(2, 0))
_btn(btn_bar, "+", grow).pack(side="right")
_btn(btn_bar, "\u2212", shrink).pack(side="right")


def _show_chrome():
    btn_bar.place(relx=1.0, y=2, x=-4, anchor="ne")
    foot.place(relx=0.0, rely=1.0, x=6, y=-2, anchor="sw")
    grip.place(relx=1.0, rely=1.0, anchor="se", x=-2, y=-2)


def _hide_chrome():
    btn_bar.place_forget()
    foot.place_forget()
    grip.place_forget()


def _check_hover():
    global hovering
    try:
        px, py = root.winfo_pointerxy()
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        inside = rx <= px < rx + w and ry <= py < ry + h
        if inside != hovering:
            hovering = inside
            if inside or drag_mode:
                _show_chrome()
            else:
                _hide_chrome()
        root.after(150, _check_hover)
    except tk.TclError:
        pass

drag_data = {"x": 0, "y": 0}


def start_drag(e):
    if drag_mode:
        drag_data["x"] = e.x_root - root.winfo_x()
        drag_data["y"] = e.y_root - root.winfo_y()


def on_drag(e):
    if drag_mode:
        root.geometry(f"+{e.x_root - drag_data['x']}+{e.y_root - drag_data['y']}")


def end_drag(_):
    if drag_mode:
        persist()


resize_data = {"w": 0, "h": 0, "x": 0, "y": 0}


def rs_start(e):
    resize_data.update(
        w=root.winfo_width(), h=root.winfo_height(),
        x=e.x_root, y=e.y_root,
    )


def rs_drag(e):
    nw = max(180, resize_data["w"] + (e.x_root - resize_data["x"]))
    nh = max(70, resize_data["h"] + (e.y_root - resize_data["y"]))
    root.geometry(f"{nw}x{nh}")


def rs_end(_):
    persist()


grip.bind("<Button-1>", rs_start)
grip.bind("<B1-Motion>", rs_drag)
grip.bind("<ButtonRelease-1>", rs_end)


def alpha_up(*_):
    global alpha
    alpha = min(1.0, round(alpha + 0.05, 2))
    root.attributes("-alpha", alpha)
    persist()


def alpha_dn(*_):
    global alpha
    alpha = max(0.15, round(alpha - 0.05, 2))
    root.attributes("-alpha", alpha)
    persist()


for w_ in (root, frame, body, cur_lbl, nxt_lbl, foot, btn_bar):
    w_.bind("<Button-1>", start_drag)
    w_.bind("<B1-Motion>", on_drag)
    w_.bind("<ButtonRelease-1>", end_drag)


def on_configure(e):
    if e.widget is root:
        wrap = max(120, root.winfo_width() - 20)
        cur_lbl.config(wraplength=wrap)
        nxt_lbl.config(wraplength=wrap)


root.bind("<Configure>", on_configure)
root.bind("<Escape>", quit_app)
root.bind("m", toggle_drag)
root.bind("M", toggle_drag)
root.bind("<bracketleft>", shrink)
root.bind("<bracketright>", grow)
root.bind("<comma>", alpha_dn)
root.bind("<period>", alpha_up)


def _poll_keys():
    """Edge-triggered global polling for arrow keys (Windows)."""
    VK_DOWN, VK_UP = 0x28, 0x26
    try:
        u32 = ctypes.windll.user32
    except Exception:
        return
    prev_d = prev_u = 0
    while True:
        try:
            d = u32.GetAsyncKeyState(VK_DOWN) & 0x8000
            u = u32.GetAsyncKeyState(VK_UP) & 0x8000
            if d and not prev_d:
                root.after(0, step_next)
            if u and not prev_u:
                root.after(0, step_prev)
            prev_d, prev_u = d, u
            time.sleep(0.04)
        except Exception:
            break


threading.Thread(target=_poll_keys, daemon=True).start()

render()
_hide_chrome()
root.after(150, _check_hover)
root.focus_force()
try:
    root.mainloop()
finally:
    persist()
