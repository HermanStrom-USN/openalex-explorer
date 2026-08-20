#!/usr/bin/env python3
"""
Build a static NVA publication snapshot for USN, bucketed by department/faculty.

Why this exists: NVA's /search/resources endpoint does not send CORS headers,
so a client-side single-file HTML tool can't call it directly from a browser
(confirmed via a live CORS test — plain fetch() fails with no HTTP response
reached, while /cristin/organization/... on the same host works fine). This
script runs server-side (in a GitHub Action, on a schedule) where CORS is
irrelevant, and commits a static JSON file that the browser tool reads instead.

Design notes, so future edits don't have to re-derive these from scratch:

- One single paginated pull of ALL USN publications (institution=222), not
  one fetch per department. NVA indexes each record against its FULL org
  ancestor chain (confirmed: a department-level record's contributorOrganizations
  includes the department, its faculty, AND the institution), so unit=<code>
  queries are already inclusive rollups. Fetching per-unit would mean
  re-fetching overlapping data ~30+ times over. One pull + local bucketing
  is both simpler and cheaper.

- A publication can belong to MULTIPLE departments at once (confirmed via a
  real 5-author, 4-faculty collaboration in USN's own data). Bucketing is
  done per contributor's own `affiliations[].id` — the exact unit tag NVA
  assigns that specific author — not by guessing a single "most specific"
  unit from the flattened contributorOrganizations list. A work is counted
  once per distinct unit among all its contributors' affiliations (same
  "one touch per distinct institution per work" rule already used for
  collaboration counts elsewhere in this project).

- DOI lives at entityDescription.reference.doi, as a full https://doi.org/...
  URL — already in the format OpenAlex's doi: filter accepts directly.
  Many records (theses, reports, Norwegian-language non-DOI content) simply
  won't have one. That's expected, not a bug — the snapshot records DOI
  coverage explicitly rather than silently dropping unmatched works.

- If the run fails partway (network error, unexpected schema, etc.), this
  script must NOT leave a truncated snapshot in place. It builds the full
  result in memory first and only writes the output file — and only lets
  the Action commit it — on a clean, complete run. A failed run should
  leave last month's good snapshot untouched and exit non-zero, loudly.

- instanceType (NVA's actual filter param — NVA's own docs call it
  "category", but the API echoes and expects "instanceType"; this script
  doesn't filter by it at fetch time at all, since we want every category
  and just record what each record actually is) is read per-record from
  entityDescription.reference.publicationInstance.type.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---- Configuration -------------------------------------------------------

USN_INSTITUTION_ID = "222"
USN_ORG_ROOT = "222.0.0.0"
API_BASE = "https://api.nva.unit.no"
# NVA's own docs ask API users to identify themselves with a contact email
# in the User-Agent header. Update this if the contact person changes.
USER_AGENT = "USN-bibliometrics-tools/1.0 (contact: herman.strom@usn.no)"

PAGE_SIZE = 100         # requested page size; the script logs what it actually gets back
MAX_PAGES = 2000        # safety cap: 2000 * 100 = 200,000 records, well above USN's ~58k total
REQUEST_TIMEOUT = 30    # seconds
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 5

OUTPUT_PATH = Path("data/nva-publications-snapshot.json")


# ---- HTTP helpers ----------------------------------------------------------

def fetch_json(url):
    """GET a URL and parse JSON, with retries on transient failures.
    Raises on anything that isn't a clean 200 after retries — callers must
    not swallow this, since a partial snapshot is worse than a failed run.
    Every raised error carries the exact URL and, for HTTP errors, the
    response body — a bare "HTTP Error 400: Bad Request" with no context
    is not debuggable, and we found that out the hard way on the first run."""
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
