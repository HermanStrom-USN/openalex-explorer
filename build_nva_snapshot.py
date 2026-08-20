#!/usr/bin/env python3
"""
Build a static NVA publication snapshot for USN, bucketed by department/faculty.

Why this exists: NVA's /search/resources endpoint does not send CORS headers,
so a client-side single-file HTML tool can't call it directly from a browser
(confirmed via a live CORS test - plain fetch() fails with no HTTP response
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
  done per contributor's own `affiliations[].id` - the exact unit tag NVA
  assigns that specific author - not by guessing a single "most specific"
  unit from the flattened contributorOrganizations list. A work is counted
  once per distinct unit among all its contributors' affiliations (same
  "one touch per distinct institution per work" rule already used for
  collaboration counts elsewhere in this project).

- DOI lives at entityDescription.reference.doi, as a full https://doi.org/...
  URL - already in the format OpenAlex's doi: filter accepts directly.
  Many records (theses, reports, Norwegian-language non-DOI content) simply
  won't have one. That's expected, not a bug - the snapshot records DOI
  coverage explicitly rather than silently dropping unmatched works.

- If the run fails partway (network error, unexpected schema, etc.), this
  script must NOT leave a truncated snapshot in place. It builds the full
  result in memory first and only writes the output file - and only lets
  the Action commit it - on a clean, complete run. A failed run should
  leave last month's good snapshot untouched and exit non-zero, loudly.

- instanceType (NVA's actual filter param - NVA's own docs call it
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
    Raises on anything that isn't a clean 200 after retries - callers must
    not swallow this, since a partial snapshot is worse than a failed run.
    Every raised error carries the exact URL and, for HTTP errors, the
    response body - a bare "HTTP Error 400: Bad Request" with no context
    is not debuggable, and we found that out the hard way on the first run."""
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                pass
            last_error = RuntimeError(f"HTTP {e.code} on {url}\nResponse body: {body}")
            if e.code == 429 or e.code >= 500:
                wait = RETRY_BACKOFF_SECONDS * attempt
                print(f"  [retry {attempt}/{RETRY_ATTEMPTS}] HTTP {e.code} on {url} - waiting {wait}s\n  body: {body[:300]}", file=sys.stderr)
                time.sleep(wait)
                continue
            raise last_error from e  # 4xx other than 429 is not going to fix itself on retry
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = RuntimeError(f"{e} on {url}")
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(f"  [retry {attempt}/{RETRY_ATTEMPTS}] {e} on {url} - waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed after {RETRY_ATTEMPTS} attempts: {url}") from last_error


# ---- Organization tree ------------------------------------------------------

def fetch_org_tree():
    """USN's full org tree comes back in one call (hasPart is nested)."""
    print("Fetching USN organization tree...")
    data = fetch_json(f"{API_BASE}/cristin/organization/{USN_ORG_ROOT}")
    return data


def flatten_org_tree(node, parent_code=None, out=None):
    """Flatten the nested hasPart tree into {code: {label_en, label_nb, acronym, parent}}."""
    if out is None:
        out = {}
    code = node["id"].rsplit("/", 1)[-1]
    out[code] = {
        "label_en": node.get("labels", {}).get("en", ""),
        "label_nb": node.get("labels", {}).get("nb", ""),
        "acronym": node.get("acronym", ""),
        "parent": parent_code,
    }
    for child in node.get("hasPart", []):
        flatten_org_tree(child, parent_code=code, out=out)
    return out


# ---- Publication pull --------------------------------------------------------

def fetch_all_publications():
    """Paginate through every USN publication using the search_after cursor
    (not plain from/size - Elasticsearch-backed search APIs like this one
    typically stop honoring deep offsets well before USN's ~58k-record total,
    which is exactly why NVA's own response includes a dedicated
    nextSearchAfterResults cursor URL).

    On a persistent 500 at the current page size, this does NOT immediately
    give up - it first tries the exact same cursor position with size=1.
    That distinguishes two very different problems:
      - If size=1 succeeds: this is a batch-size issue (something about
        serializing this many records together), not a bad record. The
        script permanently drops to the smaller page size and keeps going,
        logging loudly that it had to adapt.
      - If size=1 ALSO fails: there's a genuinely unretrievable record at
        this exact position, no page size fixes that, and continuing would
        just mean silently guessing how to skip past unknown data. The run
        aborts with the precise position and NVA's own error detail, so it
        can be reported to Sikt with something actionable rather than a
        best-effort guess baked into a snapshot."""
    records = []
    page_size = PAGE_SIZE
    base_params = f"institution={USN_INSTITUTION_ID}&sort=identifier"
    url = f"{API_BASE}/search/resources?{base_params}&size={page_size}"
    page = 0
    seen_page_sizes = set()
    degraded = False
    while url and page < MAX_PAGES:
        page += 1
        try:
            data = fetch_json(url)
        except Exception as first_error:
            # Try once more at this exact cursor position with size=1
            # before giving up, to tell a batch-size problem apart from a
            # genuinely corrupt record.
            print(f"  Page {page} failed at size={page_size}. Retrying same position at size=1 to isolate the cause...", file=sys.stderr)
            fallback_url = url.replace(f"size={page_size}", "size=1")
            try:
                data = fetch_json(fallback_url)
                print(f"  size=1 succeeded at this position - this looks like a batch-size issue, not a bad record. Dropping page size to 1 for the rest of the run.", file=sys.stderr)
                page_size = 1
                degraded = True
            except Exception as second_error:
                raise RuntimeError(
                    f"Failed on page {page} (after {len(records)} records fetched so far), "
                    f"and it still fails even at size=1 - this looks like one specific record "
                    f"NVA itself can't serve, not a batch-size problem.\n"
                    f"URL that failed (size={page_size}): {url}\n"
                    f"URL that ALSO failed (size=1): {fallback_url}\n"
                    f"This is worth reporting to Sikt with the request IDs from the error bodies above."
                ) from second_error
        hits = data.get("hits", [])
        seen_page_sizes.add(len(hits))
        records.extend(hits)
        if page == 1:
            total = data.get("totalHits", "?")
            print(f"Reported totalHits: {total}. Requested page size {page_size}, got {len(hits)} on first page.")
        if page <= 5 or page % 20 == 0:
            print(f"  ...page {page}, {len(records)} records so far. next url: {data.get('nextSearchAfterResults')
