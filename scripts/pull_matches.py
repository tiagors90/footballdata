"""
Pulls finished matches from football-data.org (free tier) and inserts new
rows into the Supabase `matches` table. Designed to run as a scheduled
GitHub Action -- see .github/workflows/pull-matches.yml.

Required environment variables (set as GitHub Actions secrets):
  SUPABASE_URL           e.g. https://myjwctfetxuyqndbnodg.supabase.co
  SUPABASE_SERVICE_KEY   the SECRET / service_role key (bypasses RLS -- never expose publicly)
  FOOTBALL_DATA_API_KEY  free key from football-data.org/client/register

What it does each run, per league with a source_code set:
  1. Reads leagues.last_pulled to know the date range to check.
  2. Fetches finished matches from football-data.org for that range.
  3. Looks up team_id/league_id by exact name match against Supabase's
     teams/leagues tables (name mismatches are logged and skipped, not
     silently dropped).
  4. Upserts matches on source_fixture_id, so re-running never duplicates.
  5. Updates leagues.last_pulled -- but never past SAFE_LAG_DAYS ago, so a
     match played later the same day this script runs can never be
     permanently skipped.

Corners and cards are NOT set by this script -- football-data.org's free
tier doesn't provide them. See backfill_corners_cards.py for that.
"""

import os
import sys
import time
from datetime import date, timedelta

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
FOOTBALL_DATA_API_KEY = os.environ["FOOTBALL_DATA_API_KEY"]

SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

FD_HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
FD_BASE = "https://api.football-data.org/v4"

DEFAULT_LOOKBACK_DAYS = 60  # first-ever run for a league
SAFE_LAG_DAYS = 3  # never mark today/recent days as "fully checked" -- matches
                    # played later the same day (after this script's run time)
                    # would otherwise get permanently skipped.


def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def sb_patch(path, body):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, json=body, timeout=20)
    r.raise_for_status()


def sb_upsert_matches(rows):
    if not rows:
        return
    headers = {**SB_HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"}
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/matches?on_conflict=source_fixture_id",
        headers=headers,
        json=rows,
        timeout=30,
    )
    if not r.ok:
        print(f"  Supabase insert failed ({r.status_code}): {r.text}")
        r.raise_for_status()


def main():
    leagues = sb_get("leagues?select=id,name,source_code,last_pulled")
    teams = sb_get("teams?select=id,name")
    team_id_by_name = {t["name"]: t["id"] for t in teams}

    try:
        aliases = sb_get("team_aliases?select=alias,team_id")
        for a in aliases:
            team_id_by_name[a["alias"]] = a["team_id"]
    except requests.HTTPError:
        pass  # table doesn't exist yet -- fine, just no aliases applied this run

    total_added = 0
    total_skipped_unmatched = 0

    for i, league in enumerate(leagues):
        code = league.get("source_code")
        if not code:
            continue  # e.g. Liga 1 (Romania) -- not covered by this API

        if i > 0:
            time.sleep(7)  # stay under football-data.org's 10 req/min free-tier limit

        last_pulled = league.get("last_pulled")
        date_from = (date.fromisoformat(last_pulled) + timedelta(days=1)) if last_pulled \
            else (date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS))
        date_to = date.today()

        if date_from > date_to:
            continue

        print(f"[{league['name']}] checking {date_from} to {date_to} ...")

        resp = requests.get(
            f"{FD_BASE}/competitions/{code}/matches",
            headers=FD_HEADERS,
            params={
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "status": "FINISHED",
                "limit": 500,  # avoid silent pagination truncation on wide date ranges
            },
            timeout=20,
        )

        if not resp.ok:
            print(f"  API error ({resp.status_code}): {resp.text}")
            continue  # don't advance last_pulled on failure

        data = resp.json()
        matches = data.get("matches", [])

        result_count = data.get("resultSet", {}).get("count")
        if result_count is not None and result_count > len(matches):
            print(f"  WARNING: API reports {result_count} total matches but only "
                  f"{len(matches)} were returned -- results may be truncated. "
                  f"Consider narrowing the date range or increasing 'limit'.")

        rows = []
        row_labels = []  # parallel list of human-readable descriptions, same order as rows
        for m in matches:
            home_name = m["homeTeam"]["name"]
            away_name = m["awayTeam"]["name"]
            home_id = team_id_by_name.get(home_name)
            away_id = team_id_by_name.get(away_name)

            if home_id is None or away_id is None:
                unknown = home_name if home_id is None else away_name
                print(f"  Skipping match -- team name not found in Supabase: '{unknown}'")
                total_skipped_unmatched += 1
                continue

            full_time = m["score"]["fullTime"]
            match_date = m["utcDate"][:10]
            rows.append({
                "match_date": match_date,
                "league_id": league["id"],
                "home_team_id": home_id,
                "away_team_id": away_id,
                "home_goals": full_time["home"],
                "away_goals": full_time["away"],
                "source_fixture_id": str(m["id"]),
            })
            row_labels.append(
                f"  [{league['name']}] {match_date}  {home_name} {full_time['home']}-{full_time['away']} {away_name}"
            )

        sb_upsert_matches(rows)
        total_added += len(rows)
        for label in row_labels:
            print(label)
        print(f"  {len(rows)} match(es) upserted.")

        # Only mark days as "fully checked" up to SAFE_LAG_DAYS ago -- never
        # the very recent days, since a match scheduled later today (after
        # this run) would otherwise get silently skipped forever.
        safe_last_pulled = min(date_to, date.today() - timedelta(days=SAFE_LAG_DAYS))
        if last_pulled is None or safe_last_pulled > date.fromisoformat(last_pulled):
            sb_patch(f"leagues?id=eq.{league['id']}", {"last_pulled": safe_last_pulled.isoformat()})

    print(f"\nDone. {total_added} match(es) upserted total, {total_skipped_unmatched} skipped for unmatched team names.")
    if total_skipped_unmatched > 0:
        print("Skipped matches usually mean a team name in Supabase doesn't exactly match football-data.org's spelling.")
        sys.exit(0)  # don't fail the Action -- this is informational, not a hard error


if __name__ == "__main__":
    main()
