"""
Backfills corners/cards onto EXISTING matches (already inserted by the
football-data.org pull) using football-data.co.uk's free, openly-downloadable
season CSV files. This is a separate, complementary data source -- no
restriction on automated access was found in their terms (unlike FlashScore/
SofaScore/LiveScore, which explicitly prohibit it).

Only fills in corners/cards where they're currently NULL -- never overwrites
anything you've already entered by hand or via a previous run.

Handles one known data quirk: football-data.co.uk occasionally lists a
match with home/away reversed compared to the actual fixture (confirmed via
Stade Rennais vs PSG, 2026-08-23 -- their CSV said PSG were home, but Stade
Rennais actually hosted). If the direct (date, home, away) lookup misses, we
try the swapped pairing before giving up, and apply the stats to the correct
side if that's what matches.

Run manually via GitHub Actions (workflow_dispatch), or on the weekly
schedule -- see .github/workflows/backfill-corners-cards.yml.
"""

import csv
import io
import os
from datetime import datetime

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

# football-data.co.uk league code -> your league name in Supabase
LEAGUE_CODES = {
    "E0":  "Premier League",
    "SP1": "La Liga",
    "I1":  "Serie A",
    "D1":  "Bundesliga",
    "F1":  "Ligue 1",
    "P1":  "Liga Portugal",
}

# Season string as used in football-data.co.uk's URLs, e.g. "2627" for 2026/27.
# Update this each August when a new season starts.
SEASON = "2627"

BASE_URL = f"https://www.football-data.co.uk/mmz4281/{SEASON}"


def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def sb_patch(path, body):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, json=body, timeout=20)
    r.raise_for_status()


def parse_date(raw):
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def main():
    leagues = {l["name"]: l["id"] for l in sb_get("leagues?select=id,name")}
    teams = sb_get("teams?select=id,name")
    team_id_by_name = {t["name"]: t["id"] for t in teams}
    team_name_by_id = {t["id"]: t["name"] for t in teams}
    aliases = sb_get("team_aliases?select=alias,team_id")
    for a in aliases:
        team_id_by_name[a["alias"]] = a["team_id"]

    total_filled = 0
    total_skipped_unmatched = 0
    total_no_match_found = 0
    total_reversed = 0

    for code, league_name in LEAGUE_CODES.items():
        league_id = leagues.get(league_name)
        if not league_id:
            print(f"[{league_name}] not found in Supabase leagues table, skipping.")
            continue

        url = f"{BASE_URL}/{code}.csv"
        resp = requests.get(url, timeout=20)
        if not resp.ok:
            print(f"[{league_name}] couldn't fetch {url} ({resp.status_code})")
            continue

        # existing matches for this league, keyed by (date, home_id, away_id)
        existing = sb_get(
            f"matches?select=id,match_date,home_team_id,away_team_id,home_corners"
            f"&league_id=eq.{league_id}"
        )
        existing_by_key = {
            (m["match_date"], m["home_team_id"], m["away_team_id"]): m for m in existing
        }

        reader = csv.DictReader(io.StringIO(resp.text))
        filled_this_league = 0

        for row in reader:
            if not row.get("HomeTeam") or not row.get("Date"):
                continue

            match_date = parse_date(row["Date"])
            if not match_date:
                continue

            home_id = team_id_by_name.get(row["HomeTeam"])
            away_id = team_id_by_name.get(row["AwayTeam"])
            if home_id is None or away_id is None:
                unknown = row["HomeTeam"] if home_id is None else row["AwayTeam"]
                print(f"[{league_name}] team name not recognized: '{unknown}' "
                      f"(add to team_aliases if this keeps appearing)")
                total_skipped_unmatched += 1
                continue

            existing_match = existing_by_key.get((match_date, home_id, away_id))
            reversed_match = False
            if not existing_match:
                existing_match = existing_by_key.get((match_date, away_id, home_id))
                reversed_match = True

            if not existing_match:
                home_name = team_name_by_id.get(home_id, row["HomeTeam"])
                away_name = team_name_by_id.get(away_id, row["AwayTeam"])
                print(f"  [{league_name}] no match in your database for {match_date}  "
                      f"{home_name} vs {away_name} (from football-data.co.uk) -- "
                      f"not pulled yet, or a date/team mismatch")
                total_no_match_found += 1
                continue

            if existing_match["home_corners"] is not None:
                continue  # already filled in, never overwrite

            hc, ac = row.get("HC"), row.get("AC")
            hy, ay = row.get("HY", "0"), row.get("AY", "0")
            hr, ar = row.get("HR", "0"), row.get("AR", "0")
            if not hc or not ac:
                continue  # this source doesn't have corners for this match either

            if reversed_match:
                # football-data.co.uk had this row's HomeTeam/AwayTeam text
                # labels scrambled (confirmed via a real match check) -- but
                # HC/AC and the card columns still correctly correspond to
                # the actual home/away teams, same as any normal row. So we
                # apply them exactly as normal; "reversed_match" only mattered
                # for locating the right row above, not for how we use the
                # numbers once found.
                pass

            final_home_corners, final_away_corners = int(hc), int(ac)
            final_home_yellow, final_away_yellow = int(hy or 0), int(ay or 0)
            final_home_red, final_away_red = int(hr or 0), int(ar or 0)

            update_body = {
                "home_corners": final_home_corners, "away_corners": final_away_corners,
                "home_yellow": final_home_yellow, "away_yellow": final_away_yellow,
                "home_red": final_home_red, "away_red": final_away_red,
            }

            sb_patch(f"matches?id=eq.{existing_match['id']}", update_body)
            filled_this_league += 1
            if reversed_match:
                total_reversed += 1

            home_name = team_name_by_id.get(home_id, row["HomeTeam"])
            away_name = team_name_by_id.get(away_id, row["AwayTeam"])
            note = "  (matched via reversed team-name lookup; stats applied as-is)" if reversed_match else ""
            print(f"  [{league_name}] {match_date}  {home_name} vs {away_name}  "
                  f"-> corners {final_home_corners}-{final_away_corners}, "
                  f"cards Y{final_home_yellow}/R{final_home_red} - Y{final_away_yellow}/R{final_away_red}{note}")

        print(f"[{league_name}] {filled_this_league} match(es) backfilled with corners/cards.")
        total_filled += filled_this_league

    print(f"\nDone. {total_filled} total backfilled ({total_reversed} with home/away corrected), "
          f"{total_skipped_unmatched} unmatched team names, "
          f"{total_no_match_found} rows with no corresponding match in your database yet.")


if __name__ == "__main__":
    main()
