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
                print(f"  [retry {attempt}/{RETRY_ATTEMPTS}] HTTP {e.code} on {url} — waiting {wait}s\n  body: {body[:300]}", file=sys.stderr)
                time.sleep(wait)
                continue
            raise last_error from e  # 4xx other than 429 is not going to fix itself on retry
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = RuntimeError(f"{e} on {url}")
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(f"  [retry {attempt}/{RETRY_ATTEMPTS}] {e} on {url} — waiting {wait}s", file=sys.stderr)
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
    (not plain from/size — Elasticsearch-backed search APIs like this one
    typically stop honoring deep offsets well before USN's ~58k-record total,
    which is exactly why NVA's own response includes a dedicated
    nextSearchAfterResults cursor URL)."""
    records = []
    url = (
        f"{API_BASE}/search/resources"
        f"?institution={USN_INSTITUTION_ID}&size={PAGE_SIZE}&sort=identifier"
    )
    page = 0
    seen_page_sizes = set()
    while url and page < MAX_PAGES:
        page += 1
        try:
            data = fetch_json(url)
        except Exception as e:
            raise RuntimeError(
                f"Failed on page {page} (after {len(records)} records fetched so far).\n"
                f"URL that failed: {url}"
            ) from e
        hits = data.get("hits", [])
        seen_page_sizes.add(len(hits))
        records.extend(hits)
        if page == 1:
            total = data.get("totalHits", "?")
            print(f"Reported totalHits: {total}. Requested page size {PAGE_SIZE}, got {len(hits)} on first page.")
        if page <= 5 or page % 20 == 0:
            print(f"  ...page {page}, {len(records)} records so far. next url: {data.get('nextSearchAfterResults')}")
        url = data.get("nextSearchAfterResults") or None
        if not hits:
            break
    print(f"Done paginating after {page} page(s). Page sizes actually observed: {sorted(seen_page_sizes)}")
    if page >= MAX_PAGES:
        raise RuntimeError(
            f"Hit MAX_PAGES safety cap ({MAX_PAGES}) without finishing — "
            f"pagination may be looping. Aborting rather than risk an infinite/partial run."
        )
    return records


# ---- Record extraction --------------------------------------------------------

def extract_record(raw):
    """Pull out exactly the fields the client tool needs, plus the set of
    distinct department/faculty units this work should be counted under."""
    entity = raw.get("entityDescription") or {}
    reference = entity.get("reference") or {}
    instance = reference.get("publicationInstance") or {}
    pub_date = entity.get("publicationDate") or {}

    units = set()
    for contributor in entity.get("contributors", []):
        for affiliation in contributor.get("affiliations", []) or []:
            unit_id = affiliation.get("id")
            if unit_id:
                units.add(unit_id.rsplit("/", 1)[-1])

    year = pub_date.get("year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None

    # NVA's "doi" field is not strictly validated on their end — real records
    # have been seen with non-DOI URLs in it (e.g. a plain Instagram link on
    # an "OtherPresentation" record). Trusting it blindly would inflate DOI
    # coverage numbers and silently fail later when matching against
    # OpenAlex. Only accept values that actually look like a DOI resolver URL.
    raw_doi = reference.get("doi")
    doi = raw_doi if raw_doi and raw_doi.startswith("https://doi.org/") else None
    doi_field_present_but_invalid = bool(raw_doi) and doi is None

    return {
        "id": raw.get("id"),
        "identifier": raw.get("identifier"),
        "title": entity.get("mainTitle"),
        "year": year,
        "type": instance.get("type"),
        "doi": doi,
        "doi_field_present_but_invalid": doi_field_present_but_invalid,
        "units": sorted(units),
    }


# ---- Main ------------------------------------------------------------------

def main():
    org_tree_raw = fetch_org_tree()
    org_flat = flatten_org_tree(org_tree_raw)
    print(f"Flattened {len(org_flat)} organizational units under USN.")

    raw_records = fetch_all_publications()
    print(f"Fetched {len(raw_records)} raw publication records.")

    publications = [extract_record(r) for r in raw_records]

    # Sanity checks — surfaced loudly, not silently absorbed. A snapshot that
    # looks structurally fine but has, say, near-zero DOI coverage or a pile
    # of untagged units is a sign something about the extraction broke.
    with_doi = sum(1 for p in publications if p["doi"])
    with_invalid_doi_field = sum(1 for p in publications if p["doi_field_present_but_invalid"])
    with_no_unit = sum(1 for p in publications if not p["units"])
    by_type = {}
    for p in publications:
        by_type[p["type"] or "Unknown"] = by_type.get(p["type"] or "Unknown", 0) + 1

    print(f"DOI coverage: {with_doi}/{len(publications)} ({100*with_doi/len(publications):.1f}%)")
    print(f"Records with a non-DOI value in the doi field (discarded, not counted as DOI coverage): {with_invalid_doi_field}")
    print(f"Records with NO recognized unit tag: {with_no_unit}")
    print("Breakdown by type:")
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {t}: {n}")

    if len(publications) == 0:
        raise RuntimeError("Fetched zero publications — refusing to write an empty snapshot over a good one.")
    if with_no_unit > len(publications) * 0.5:
        raise RuntimeError(
            f"Over half of records ({with_no_unit}/{len(publications)}) have no recognized unit tag — "
            f"this smells like a schema change upstream, not normal data. Aborting rather than "
            f"shipping a snapshot that can't actually be browsed by department."
        )

    # Precompute unit -> [publication index] for fast client-side lookup,
    # so the browser tool doesn't have to scan every publication per view.
    by_unit = {}
    for idx, p in enumerate(publications):
        for unit in p["units"]:
            by_unit.setdefault(unit, []).append(idx)

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "NVA (api.nva.unit.no/search/resources), institution=222",
        "institution_total_publications": len(publications),
        "doi_coverage": {"with_doi": with_doi, "total": len(publications)},
        "organizations": org_flat,
        "publications": publications,
        "by_unit": by_unit,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=None, separators=(",", ":")))
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1024 / 1024:.2f} MB)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FAILED. Full error chain:", file=sys.stderr)
        current = e
        depth = 0
        while current is not None:
            print(f"  [{depth}] {type(current).__name__}: {current}", file=sys.stderr)
            current = current.__cause__
            depth += 1
        sys.exit(1)
