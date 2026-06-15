import tempfile

from poe2market.tracker import (
    BootAttrs,
    PollResult,
    TrackerStore,
    boot_attrs,
    defence_type,
    _correction,
    _ms_tier,
)


# ---- mod bucketing ----------------------------------------------------------

def _item(type_line="Quickslip Shoes", props=None, expl=None, runes=None,
          crafted=None, desecrated=None, sockets=2, corrupted=False):
    return {
        "typeLine": type_line,
        "properties": props if props is not None else [
            {"name": "[Evasion|Evasion Rating]"}, {"name": "[EnergyShield|Energy Shield]"}],
        "explicitMods": expl or [],
        "runeMods": runes or [],
        "craftedMods": crafted or [],
        "desecratedMods": desecrated or [],
        "sockets": [{}] * sockets,
        "corrupted": corrupted,
    }


def test_defence_type_from_properties():
    assert defence_type(_item(props=[{"name": "[Evasion|Evasion Rating]"}])) == "EV"
    assert defence_type(_item(props=[{"name": "Energy Shield"}])) == "ES"
    assert defence_type(_item(props=[
        {"name": "[Evasion|Evasion Rating]"}, {"name": "Energy Shield"}])) == "EV+ES"


def test_ms_tier_counts_rune_as_pseudo():
    # 30% explicit + 5% rune = 35 pseudo -> tier "35"
    a = boot_attrs(_item(expl=["30% increased Movement Speed"],
                         runes=["5% increased Movement Speed"]))
    assert a.ms == 35
    assert _ms_tier(a.ms) == "35"
    # 35% explicit + 5% rune = 40 pseudo -> above-cap "36+"
    a2 = boot_attrs(_item(expl=["35% increased Movement Speed"],
                          runes=["5% increased Movement Speed"]))
    assert a2.ms == 40 and _ms_tier(a2.ms) == "36+"


def test_res_includes_rune_and_signature():
    a = boot_attrs(_item(
        type_line="Quickslip Shoes",
        expl=["35% increased Movement Speed", "+45% to Cold Resistance",
              "+30% to Lightning Resistance", "+120 to maximum Life"],
        runes=["+18% to Fire Resistance"],
        crafted=["18% increased Rarity of Items found"]))
    assert a.res == 45 + 30 + 18           # explicit + rune
    assert a.life == 120 and a.rarity == 18
    assert a.signature() == "EV+ES|2s|ms35|res70-99|life120+|rarY"


def test_runeforged_detected_from_name_or_ward():
    assert boot_attrs(_item(type_line="Runeforged Quickslip Shoes")).runeforged
    assert boot_attrs(_item(props=[{"name": "[RunicWard|Runic Ward]"},
                                   {"name": "Energy Shield"}])).runeforged


# ---- lifecycle state machine (no network) -----------------------------------

def _store():
    return TrackerStore(tempfile.mkdtemp())


def _attrs(sig_sockets=2):
    a = boot_attrs(_item(
        sockets=sig_sockets,
        expl=["35% increased Movement Speed", "+45% to Cold Resistance",
              "+30% to Lightning Resistance"]))
    return a


def _record(store, league, item_hash, price_ex, poll_ts, attrs=None):
    res = PollResult(league=league, poll_ts=poll_ts)
    store.record_offline(league, [(item_hash, attrs or _attrs(), price_ex, price_ex,
                                   "exalted", "")], poll_ts, res)
    return res


def test_new_then_reprice_down_then_gone():
    s = _store()
    lg = "L"
    # poll 1: listed at 4000ex
    r1 = _record(s, lg, "boot-A", 4000.0, 100.0)
    assert r1.new == 1
    # poll 2: repriced DOWN to 3000ex (seller couldn't sell at 4000)
    r2 = _record(s, lg, "boot-A", 3000.0, 200.0)
    assert r2.repriced_down == 1 and r2.new == 0
    # poll 3: absent (seller removed it) -> missed=1, not gone yet
    res3 = PollResult(league=lg, poll_ts=300.0)
    s.mark_absent(lg, set(), 300.0, res3)
    assert res3.marked_gone == 0
    # poll 4: still absent -> GONE_AFTER=2 reached -> exit price = last seen (3000)
    res4 = PollResult(league=lg, poll_ts=400.0)
    s.mark_absent(lg, set(), 400.0, res4)
    assert res4.marked_gone == 1
    rep = s.clearing_report(lg, min_exits=1)
    assert len(rep) == 1
    assert rep[0]["n_exits"] == 1
    assert rep[0]["median_exit_ex"] == 3000.0      # exit at the repriced-down price
    assert rep[0]["reprice_down_rate"] == 1.0


def test_reappearing_listing_is_revived_not_double_counted():
    s = _store()
    lg = "L"
    _record(s, lg, "boot-B", 5000.0, 10.0)
    # missed one poll
    s.mark_absent(lg, set(), 20.0, PollResult(league=lg, poll_ts=20.0))
    # seen again before hitting GONE_AFTER -> back to active, missed reset
    _record(s, lg, "boot-B", 5000.0, 30.0)
    res = PollResult(league=lg, poll_ts=40.0)
    s.mark_absent(lg, {"boot-B"}, 40.0, res)  # seen this poll
    assert res.marked_gone == 0
    assert s.summary(lg)["active"] == 1 and s.summary(lg)["gone"] == 0


def test_report_filters_by_defence_and_min_exits():
    s = _store()
    lg = "L"
    ev_es = _attrs()                       # EV+ES (in focus)
    ar_item = boot_attrs(_item(props=[{"name": "[Armour]"}],
                               expl=["35% increased Movement Speed"], sockets=1))
    # one EV+ES exit
    _record(s, lg, "x1", 2000.0, 1.0, ev_es)
    s.mark_absent(lg, set(), 2.0, PollResult(league=lg, poll_ts=2.0))
    s.mark_absent(lg, set(), 3.0, PollResult(league=lg, poll_ts=3.0))
    # one AR exit (out of focus — armour is ignored)
    _record(s, lg, "x2", 9000.0, 1.0, ar_item)
    s.mark_absent(lg, set(), 2.0, PollResult(league=lg, poll_ts=2.0))
    s.mark_absent(lg, set(), 3.0, PollResult(league=lg, poll_ts=3.0))
    # focus is ES/EV/EV+ES, min_exits=1 -> the EV+ES bucket shows, AR excluded
    rep = s.clearing_report(lg, min_exits=1)
    assert any(r["mod_sig"].startswith("EV+ES") for r in rep)
    assert not any(r["mod_sig"].startswith("AR") for r in rep)


def test_correction_factor_geomean():
    # exits are asking-at-disappearance; sold are real (lower). Two matched
    # buckets at 0.5x and 0.8x -> geomean ~0.632; a third bucket only in one
    # side is ignored.
    exits = {"A": 100.0, "B": 100.0, "C": 100.0}
    sold = {"A": 50.0, "B": 80.0, "D": 999.0}
    out = _correction(exits, sold)
    assert out["n_matched"] == 2
    assert abs(out["global_factor"] - (0.5 * 0.8) ** 0.5) < 1e-9
    # ratios carried per-bucket, sorted by sold desc
    assert out["pairs"][0]["mod_sig"] == "B"


def test_correction_no_overlap_is_identity():
    out = _correction({"A": 10.0}, {"B": 20.0})
    assert out["n_matched"] == 0 and out["global_factor"] == 1.0
