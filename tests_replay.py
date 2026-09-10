"""Offline replay test: uses cached ESPN payloads (fixtures/) to verify
formatter & fantasy logic against the reference channel examples."""
import json
import os

import formatter as F
import fantasy as FY

HERE = os.path.dirname(os.path.abspath(__file__))


def load(eid):
    with open(os.path.join(HERE, "fixtures", f"summary_{eid}.json")) as f:
        return json.load(f)


def _nm(m):
    m = str(m or "").strip().replace("'", "")
    return m + "'" if m else m


def summarize(data):
    comp = (data.get("header", {}).get("competitions") or [{}])[0]
    competitors = {}
    for c in comp.get("competitors", []):
        t = c.get("team", {})
        competitors[c.get("homeAway")] = {
            "id": str(c.get("id")), "abbr": t.get("abbreviation", "?"),
            "name": t.get("displayName", "?"), "short": t.get("shortDisplayName", "?"),
            "color": (t.get("color") or "").lstrip("#"),
            "score": int(str(c.get("score", "0") or "0")),
        }
    keys = []
    for k in data.get("keyEvents", []):
        typ = k.get("type", {}) or {}
        keys.append({
            "id": str(k.get("id")), "type": typ.get("text", ""),
            "minute": _nm((k.get("clock") or {}).get("displayValue", "")),
            "players": [(p.get("athlete") or {}).get("displayName", "") for p in (k.get("participants") or [])],
            "team_id": str((k.get("team") or {}).get("id") or ""),
            "text": k.get("text", ""),
        })
    rosters = []
    for rb in data.get("rosters", []):
        entries = []
        for e in rb.get("roster", []):
            stats = {s.get("name"): s.get("value") or 0 for s in e.get("stats", []) if s.get("name")}
            entries.append({"player": (e.get("athlete") or {}).get("displayName", "?"),
                            "pos": ((e.get("position") or {}).get("abbreviation") or ""),
                            "starter": bool(e.get("starter")), "subbed_in": bool(e.get("subbedIn")),
                            "subbed_out": bool(e.get("subbedOut")), "stats": stats})
        rosters.append({"team_id": str((rb.get("team") or {}).get("id") or ""), "entries": entries})
    tstats = [{"team_id": str((t.get("team") or {}).get("id") or ""),
               "stats": {s.get("name"): s.get("displayValue") for s in (t.get("statistics") or [])}}
              for t in (data.get("boxscore", {}) or {}).get("teams", [])]
    return competitors, keys, rosters, tstats


def find(kevent_list, **conds):
    out = []
    for k in kevent_list:
        ok = True
        for kk, vv in conds.items():
            if kk == "type":
                ok = ok and k["type"].lower() == vv.lower()
            elif kk == "player_has":
                ok = ok and any(vv.lower() in p.lower() for p in k["players"])
            elif kk == "minute":
                ok = ok and k["minute"] == vv
        if ok:
            out.append(k)
    return out


print("=" * 70)
data = load("401915445")  # PSG 6-1 Slovan
comp, keys, rosters, tstats = summarize(data)
print("MATCH:", comp.get("home", {}).get("name"), "vs", comp.get("away", {}).get("name"))
print()

# TEST 1: goal with assist -> reference style (⚽ ØDEGAARD / 🅰️ TZOLIS)
g = find(keys, type="goal", player_has="Akliouche")[0]
side = F.team_side_emoji(comp, g["team_id"])[1] or "home"
scorer = F.event_lastname_upper(g["players"][0])
assist = g["players"][1]
txt = F.render_goal(comp, side, scorer, assist, g["minute"])
print("--- TEST 1: goal+assist ---")
print(txt)
assert "⚽ FERRAN" in txt and "🅰️ AKLIOUCHE" in txt
assert "⌚️ 47' |" in txt and "#PSGSLB | Live" in txt
print("[PASS]\n")

# TEST 2: goal without assist
g2 = find(keys, type="goal", player_has="Fabián")[0]
txt2 = F.render_goal(comp, F.team_side_emoji(comp, g2["team_id"])[1],
                     F.event_lastname_upper(g2["players"][0]), None, g2["minute"])
print("--- TEST 2: goal without assist (Fabián) ---")
print(txt2)
assert "⚽ FABIÁN" in txt2 and "🅰️" not in txt2
print("[PASS]\n")

# TEST 3: yellow card format
y = find(keys, type="yellow card")[0]
txt3 = F.render_card(comp, y["team_id"], F.event_lastname_upper(y["players"][0]), "yellow", y["minute"])
print("--- TEST 3: yellow ---")
print(txt3)
assert "🟨 BLACKMAN YELLOW" in txt3 and "#PSGSLB | Live" in txt3
print("[PASS]\n")

# TEST 4: disallowed format (reference: ❌ GOAL DISALLOWED | OFFSIDE)
txt4 = F.render_disallowed(comp, "57'", "OFFSIDE")
print("--- TEST 4: disallowed ---")
print(txt4)
assert "❌ GOAL DISALLOWED | OFFSIDE" in txt4
assert "⌚️ 57' | 🔵 PSG - SLB 🔵" in txt4  # both teams blue, same as reference channel
print("[PASS]\n")

# TEST 5: bracket marks the scoring side
txt5 = F.render_goal(comp, "home", "DEMBÉLÉ", None, "17'")
print("--- TEST 5: brackets ---")
print(txt5)
assert "🔵 PSG [6] - 1 SLB 🔵" in txt5  # reference itself shows both blue: '🔵 PSG 6 - 1 SBR 🔵'
txt5b = F.render_goal(comp, "away", "SULEIMAN", None, "59'")
assert "🔵 PSG 6 - [1] SLB 🔵" in txt5b
print("[PASS]\n")

# TEST 6a: structural unit check of the formula (synthetic, exact)
synthetic = [{"player": "Test Man", "pos": "F", "starter": True, "subbed_in": False,
              "subbed_out": False,
              "stats": {"totalGoals": 2.0, "goalAssists": 2.0, "shotsOnTarget": 2.0,
                        "yellowCards": 0.0, "redCards": 0.0, "ownGoals": 0.0,
                        "goalsConceded": 0.0, "saves": 0.0}}]
assert FY.calc(synthetic[0], {}) == 16  # 2app+8+4+2  == official Dembélé style
sub60 = [{"player": "Early Sub", "pos": "M", "starter": True, "subbed_in": False,
          "subbed_out": True, "stats": {"totalGoals": 0.0, "goalAssists": 0.0,
                                        "shotsOnTarget": 0.0, "goalsConceded": 0.0,
                                        "saves": 0.0}}]
subs = {"early sub": [None, 45]}
assert FY.calc(sub60[0], subs) == 1  # <60min appearance = 1pt
print("[PASS] formula structure\n")

# TEST 6: fantasy points vs official sample (ESPN vs Opta tallies allow ±2)
print("--- TEST 6: fantasy points (official: Dembélé 16, Nuno Mendes 5, Fabián 7) ---")
subs_map = FY.sub_minutes(keys)
rows = {}
for rb in rosters:
    for e in rb["entries"]:
        p = FY.calc(e, subs_map)
        if p:
            rows[e["player"]] = p
expect = {"Ousmane Dembélé": 16, "Fabián Ruiz": 7, "Suleiman Camara": 7, "Ferran Torres": 15}
ok = True
for name, exp in expect.items():
    got = rows.get(name)
    print(f"{name}: {got} (official {exp}, diff {None if got is None else got-exp})")
    ok = ok and got is not None and abs(got - exp) <= 2
assert ok, "fantasy drift > 2 points"
assert rows.get("Fabián Ruiz") == 7
block = FY.points_block(rosters, key_events=keys)
print("points block head:", block[:4])
assert any("O. Dembélé" in l for l in block)
print("[PASS]\n")

# TEST 7: FT block with points + stats
pts = FY.points_block(rosters, key_events=keys)
stats_txt = F.render_stats(comp, tstats)
ft = F.render_ft(comp, pts, None, stats_txt)
print("--- TEST 7: FT block (first 20 lines) ---")
print("\n".join(ft.split("\n")[:20]))
assert "🏁 FT |" in ft
assert "O. Dembélé" in ft and "📊 Stats" in ft
assert "D. Takac" not in ft  # only goal contributors listed (reference style)
print("[PASS]\n")

# TEST 8: lineups rendering (NAP-ARS)
data2 = load("401915423")
comp2, keys2, rosters2, _ = summarize(data2)
lu = F.render_lineups(comp2, rosters2)
print("--- TEST 8: lineups head ---")
print("\n".join(lu.split("\n")[:4]))
assert "XI:" in lu
print("[PASS]\n")

# TEST 9: Ødegaard yellow (reference: 🟨 ØDEGAARD YELLOW (90+1') 🔴 ARS)
y2 = find(keys2, type="yellow card", player_has="degaard")
assert y2, "Odegaard yellow not found in fixture"
yy = y2[0]
txt9 = F.render_card(comp2, yy["team_id"], F.event_lastname_upper(yy["players"][0]), "yellow", yy["minute"])
print("--- TEST 9: Odegaard yellow ---")
print(txt9)
assert "🟨 ØDEGAARD YELLOW (90+1')" in txt9
print("[PASS]\n")

# TEST 10: penalty outcome formats (synthetic from reference)
pen = F.render_penalty_outcome(comp, "SUÁREZ", "pen_saved", "54'")
print("--- TEST 10: penalty saved ---")
print(pen)
assert "🧤 PENALTY SAVED — SUÁREZ" in pen
penw = F.render_penalty_awarded(comp, str(comp["away"]["id"]), "SUÁREZ", "54'")
print("--- TEST 10b: penalty awarded ---")
print(penw)
assert "🥅 PENALTY FOR" in penw and "SUÁREZ" in penw
print("[PASS]\n")

print("=" * 70)
print("ALL FORMATTER/FANTASY TESTS PASSED ✅")
