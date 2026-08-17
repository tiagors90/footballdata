"""
Proactively checks football-data.org's official team names for every league
against Supabase, WITHOUT needing any matches to have been played yet (uses
the /teams endpoint, not /matches). Prints ready-to-paste alias SQL for any
mismatches found.

Run manually via GitHub Actions (workflow_dispatch) -- see
.github/workflows/check-team-names.yml. Read-only: makes no changes to the
database itself, just prints suggestions to the log.
"""

import difflib
import os
import time

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
FOOTBALL_DATA_API_KEY = os.environ["FOOTBALL_DATA_API_KEY"]

SB_HEADERS = {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"}
FD_HEADERS = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
FD_BASE = "https://api.football-data.org/v4"


def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def main():
    leagues = sb_get("leagues?select=id,name,source_code")
    aliases = sb_get("team_aliases?select=alias,team_id")
    alias_names = {a["alias"] for a in aliases}

    total_mismatches = 0

    for i, league in enumerate(leagues):
        code = league.get("source_code")
        if not code:
            continue

        if i > 0:
            time.sleep(7)  # stay under football-data.org's free-tier rate limit

        local_teams = sb_get(f"teams?select=id,name&league_id=eq.{league['id']}")
        local_names = {t["name"] for t in local_teams}
        known_names = local_names | alias_names

        resp = requests.get(f"{FD_BASE}/competitions/{code}/teams", headers=FD_HEADERS, timeout=20)
        if not resp.ok:
            print(f"[{league['name']}] API error ({resp.status_code}): {resp.text}")
            continue

        api_teams = resp.json().get("teams", [])
        mismatches = [t["name"] for t in api_teams if t["name"] not in known_names]

        print(f"\n[{league['name']}] {len(api_teams)} teams on football-data.org, "
              f"{len(mismatches)} not recognized locally.")

        if not mismatches:
            continue

        print(f"-- Suggested aliases for {league['name']} -- REVIEW before running, don't blindly trust the guesses:")
        print("insert into team_aliases (alias, team_id) values")
        lines = []
        for api_name in mismatches:
            best = difflib.get_close_matches(api_name, local_names, n=1, cutoff=0.3)
            guess = best[0] if best else "UNKNOWN -- pick manually"
            lines.append(
                f"  ('{api_name}', (select id from teams where name = '{guess}'))  -- guessed match: {guess}"
            )
            total_mismatches += 1
        print(",\n".join(lines) + "\non conflict (alias) do nothing;")

    print(f"\nDone. {total_mismatches} total suggested aliases across all leagues checked.")


if __name__ == "__main__":
    main()
