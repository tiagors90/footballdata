"""
Deletes access_log rows older than the retention window. Run daily via
GitHub Actions -- see .github/workflows/cleanup-access-log.yml.

This script is what makes the 60-day retention claim in the site's privacy
notice actually true, rather than aspirational -- it needs to keep running
for that statement to remain accurate.
"""

import os
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

RETENTION_DAYS = 60

SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Prefer": "count=exact",
}


def main():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()

    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/access_log",
        headers=SB_HEADERS,
        params={"ts": f"lt.{cutoff}"},
        timeout=30,
    )
    r.raise_for_status()

    # PostgREST reports the affected row count via the Content-Range header,
    # e.g. "*/47" or "0-46/47", when Prefer: count=exact is set.
    content_range = r.headers.get("Content-Range", "")
    count = content_range.split("/")[-1] if "/" in content_range else "unknown"

    print(f"Deleted access_log rows older than {cutoff} (retention: {RETENTION_DAYS} days). "
          f"Rows removed: {count}")


if __name__ == "__main__":
    main()
