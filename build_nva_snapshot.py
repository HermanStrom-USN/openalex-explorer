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

--- Added: channel level, NVI points, co-publishing, open-access inference ---

- Channel level ("nivå") lives at entityDescription.reference.publicationContext
  .scientificValue for journal/series contexts, or nested one level deeper
  under .publisher.scientificValue for publisher-issued works (books, reports).
  Checked in that order, falling back to .series.scientificValue as a third
  case, since a single publicationContext can carry both a series and a
  publisher (e.g. a monograph issued in a numbered series) and it's the
  publisher's rating that governs points for monograph-form works. This
  fallback chain is based on the one example schema NVA publishes (see
  https://github.com/BIBSYSDEV/nva-search-api, /resource/README.md) —
  the printed "channel level coverage" stat below is there specifically so
  a structural mismatch (e.g. a context shape this chain doesn't handle)
  shows up as a coverage drop immediately, not silently.

- NVI points use the official DBH weight table (confirmed against
  https://dbh.hkdir.no/dbh-old/dokumentasjon/tabell.action?tabellId=373) and
  the post-2015 formula (confirmed against Cristin's own methodology page
  and matching descriptions from UiO/UiB library documentation):
      points = weight(form, level) x sqrt(n/N) x (1.3 if international else 1)
  where N is the publication's total "author shares" (every unique
  author x institution combination) and n is USN's own share of those.

  The NVA-category -> weight-table-row mapping (NVI_FORM_BY_CATEGORY below)
  is a STARTING ASSUMPTION flagged for confirmation, not verified against
  USN's actual category usage — cross-check the "records by NVI form" stat
  this script prints against what USN's own reporting expects before
  treating computed points as authoritative in anything institutional.

- Author-institution shares are computed by parsing each contributor's
  affiliation IDs down to their institution root (Cristin codes are
  INSTITUTION.FACULTY.DEPT.GROUP — e.g. "222.3.10.0" — so the root is just
  the first segment with the rest zeroed). This needs no API lookup and
  works for any institution, not just USN's own tree.

- Distinct non-USN institution roots encountered anywhere in the dataset are
  resolved once (label + country) via the same /cristin/organization/{id}
  endpoint already used for USN's own tree, and stored in a separate
  `partner_organizations` dict — publications reference these by code, the
  same way they already reference `organizations` via `units`, rather than
  duplicating label strings across every publication record. A single
  partner org failing to resolve is logged and degrades to showing its raw
  code rather than failing the whole run — unlike the main publication
  fetch, a missing partner label is a cosmetic gap, not a structural
  problem, so it doesn't warrant aborting.

- Open access has no dedicated field in NVA's schema. It's inferred from
  whether the record has at least one associatedArtifact that is a publicly
  visible file with a license attached — a heuristic, not an authoritative
  status the way OpenAlex provides one, and it should be labeled as such
  wherever it's surfaced in the client tool.
"""

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---- Configuration -------------------------------------------------------

USN_INSTITUTION_ID = "222"

# Which institution this run builds a snapshot for — set via the NVA_INSTITUTION_ID env
# var (see the matrix workflow, nva-snapshot-multi.yml) so the same script builds every
# institution's snapshot, one per parallel job, rather than needing a separate copy of
# this file per university. Defaults to USN so a plain local run behaves exactly as
# before if the env var isn't set.
INSTITUTION_ID = os.environ.get("NVA_INSTITUTION_ID", USN_INSTITUTION_ID)
INSTITUTION_ORG_ROOT = f"{INSTITUTION_ID}.0.0.0"
API_BASE = "https://api.nva.unit.no"
# NVA's own docs ask API users to identify themselves with a contact email
# in the User-Agent header. Update this if the contact person changes.
USER_AGENT = "USN-bibliometrics-tools/1.0 (contact: herman.strom@usn.no)"

PAGE_SIZE = 100         # requested page size; the script logs what it actually gets back
MAX_PAGES = 2000        # safety cap: 2000 * 100 = 200,000 records, well above USN's ~58k total
REQUEST_TIMEOUT = 30    # seconds
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 5

# One file per institution — data/nva-snapshot-222.json for USN, -194 for NTNU, etc. —
# not one combined file. A single merged file covering every Norwegian university risks
# exceeding GitHub's 100MB per-file limit outright (UiO and NTNU alone are each likely
# several times USN's own size), and the browser tool only ever needs to load whichever
# institution's file is actually selected for a given comparison, not all of them.
OUTPUT_PATH = Path(f"data/nva-snapshot-{INSTITUTION_ID}.json")

# Official DBH weight table (Publikasjonsform x Niva). Source:
# https://dbh.hkdir.no/dbh-old/dokumentasjon/tabell.action?tabellId=373
NVI_WEIGHTS = {
    "article":           {"LevelOne": 1.0, "LevelTwo": 3.0},
    "anthology_chapter": {"LevelOne": 0.7, "LevelTwo": 1.0},
    "monograph":         {"LevelOne": 5.0, "LevelTwo": 8.0},
}

# NVA instanceType -> which row of NVI_WEIGHTS applies. STARTING ASSUMPTION,
# not confirmed against USN's real category usage — see module docstring.
# Every category not listed here is treated as not NVI-eligible (0 points
# regardless of channel level), which is correct for the great majority of
# NVA's ~50 categories (conference material, media pieces, degree theses,
# etc.) but should be spot-checked against the printed coverage stats.
NVI_FORM_BY_CATEGORY = {
    "AcademicArticle": "article",
    "AcademicLiteratureReview": "article",
    "AcademicChapter": "anthology_chapter",
    "AcademicMonograph": "monograph",
}


# ---- HTTP helpers ----------------------------------------------------------

def fetch_json(url):
    """GET a URL and parse JSON, with retries on transient failures.
    Raises on anything that isn't a clean 200 after retries — callers must
    not swallow this, since a partial snapshot is worse than a failed run."""
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
            last_error = e
            # Read the response body before it's discarded — REST APIs commonly return a
            # JSON body on 4xx/5xx explaining exactly what was wrong with the request, and
            # a bare status line ("HTTP Error 400: Bad Request") throws that away. This is
            # what turns an unactionable mystery failure into one that says what's wrong.
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = "(could not read response body)"
            if e.code == 429 or e.code >= 500:
                wait = RETRY_BACKOFF_SECONDS * attempt
                print(f"  [retry {attempt}/{RETRY_ATTEMPTS}] HTTP {e.code} on {url} — waiting {wait}s", file=sys.stderr)
                print(f"    Response body: {body[:500]}", file=sys.stderr)
                time.sleep(wait)
                continue
            # 4xx other than 429 is not going to fix itself on retry — but include the body
            # so whoever reads this knows *why*, not just *that* it failed.
            raise RuntimeError(f"HTTP {e.code} on {url}\nResponse body: {body[:2000]}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e
            wait = RETRY_BACKOFF_SECONDS * attempt
            print(f"  [retry {attempt}/{RETRY_ATTEMPTS}] {e} on {url} — waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed after {RETRY_ATTEMPTS} attempts: {url}") from last_error


# ---- Organization tree ------------------------------------------------------

def fetch_org_tree():
    """USN's full org tree comes back in one call (hasPart is nested)."""
    print("Fetching USN organization tree...")
    data = fetch_json(f"{API_BASE}/cristin/organization/{INSTITUTION_ORG_ROOT}")
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


def institution_root(affiliation_id):
    """A Cristin org code is INSTITUTION.FACULTY.DEPT.GROUP (e.g. "222.3.10.0") —
    the root institution is always the first segment with the rest zeroed, no
    API lookup needed. Handles both bare codes and full
    https://.../cristin/organization/<code> URIs. Returns None for anything
    that doesn't look like a Cristin code, rather than guessing."""
    if not affiliation_id:
        return None
    code = affiliation_id.rsplit("/", 1)[-1]
    first = code.split(".")[0]
    return f"{first}.0.0.0" if first.isdigit() else None


def resolve_partner_organizations(root_codes):
    """Resolves label + country for each distinct non-USN institution root
    encountered anywhere in the dataset. One fetch per institution (root_codes
    are already institution-level, not per-department), so this stays cheap
    even across USN's full publication history — the number of DISTINCT
    collaborating institutions is nowhere near USN's own record count.

    A first real run resolved 3,014 partner institutions but the
    international-collaboration flag came back 0/57,860 — implausible for a
    research university, meaning countryCode isn't actually the field this
    endpoint returns country under. Rather than guess a second time, this
    prints the real raw shape of the first successfully resolved org, once,
    so the next run shows us the truth directly."""
    resolved = {}
    codes = sorted(c for c in root_codes if c)
    print(f"Resolving {len(codes)} distinct partner institution(s)...")
    diagnostic_printed = False
    for i, code in enumerate(codes, 1):
        try:
            data = fetch_json(f"{API_BASE}/cristin/organization/{code}")
            if not diagnostic_printed:
                diagnostic_printed = True
                print(f"DIAGNOSTIC: raw response shape for first partner org ({code}):", file=sys.stderr)
                print(f"  Top-level keys: {sorted(data.keys())}", file=sys.stderr)
                print(f"  Full response (truncated to 1500 chars): {json.dumps(data, indent=2, ensure_ascii=False)[:1500]}", file=sys.stderr)
            labels = data.get("labels", {}) or {}
            resolved[code] = {
                "label_en": labels.get("en") or labels.get("nb") or code,
                "label_nb": labels.get("nb", ""),
                # Confirmed via a real diagnostic print: the field is "country", not
                # "countryCode" — the wrong guess silently left every partner org's
                # country as None, which meant nothing ever counted as international.
                "country_code": data.get("country"),
            }
        except Exception as e:
            # A single partner org failing to resolve is a cosmetic gap (it'll show as a
            # raw code instead of a name), not a structural problem — unlike the main
            # publication fetch, this doesn't warrant aborting the whole run over.
            print(f"  [warn] Could not resolve partner org {code}: {e} — will show as code only.", file=sys.stderr)
            resolved[code] = {"label_en": code, "label_nb": code, "country_code": None}
        if i % 50 == 0:
            print(f"  ...resolved {i}/{len(codes)}")
    return resolved


# ---- Publication pull --------------------------------------------------------

YEAR_MIN = 1970  # generous lower bound — years with zero results cost one fast, empty
                  # request each, so an overly wide range is cheap; too narrow would
                  # silently drop real data. Widen if the cross-check below flags a gap.

# If a page fails even after fetch_json's own retries at the normal PAGE_SIZE, these are
# tried in order, from the exact same search_after position, before giving up on that
# position entirely. Confirmed against a real reproducible case (years 1970-2024 fetched
# cleanly at size=100; year 2025 failed identically 4 times at size=100 on its second
# page) — the theory is that NVA's 500 is tied to processing/serializing a full batch
# together (a resource or timeout limit, possibly compounded by one problematic record's
# effect across the batch) rather than one permanently unfetchable record, so a much
# smaller batch from the same position may simply succeed. Smallest-last so the common
# case (PAGE_SIZE just works) pays no extra cost.
FALLBACK_PAGE_SIZES = (20, 5, 1)


def fetch_true_total_count():
    """A single lightweight call (size=1, never paginated) purely to read NVA's own
    authoritative totalHits for institution=222 — this exact call already worked cleanly
    (it's how "Reported totalHits: 57869" showed up before deeper pagination hit trouble),
    so it's safe to keep using as a cross-check reference: after slicing the real pull by
    year below, comparing the summed count against this number is what would catch a year
    range that's too narrow, rather than silently shipping an incomplete snapshot."""
    url = f"{API_BASE}/search/resources?institution={INSTITUTION_ID}&size=1&sort=identifier"
    data = fetch_json(url)
    return data.get("totalHits")


def fetch_publications_for_year(year):
    """Paginate through USN publications for a single year, using the same search_after +
    sort=identifier mechanism as before, just scoped to one year at a time.

    If a page fails even after fetch_json's own retries, progressively smaller page sizes
    (FALLBACK_PAGE_SIZES) are tried from the exact same position before giving up — see
    that constant's comment for why this is a reasonable thing to try, not a wild guess.

    If even size=1 still fails at some position, this year's fetch STOPS there rather than
    raising and losing every record already fetched for the year: whatever was gathered
    before the block is kept, and the blocking point is returned (as a URL, for
    diagnostics) so the caller can record the gap explicitly rather than the snapshot
    silently being short. This is a deliberate, narrow exception to this script's normal
    "no partial data, ever" principle (see module docstring) — justified specifically
    because it only engages after exhausting every other option (fetch_json's own retries,
    then three smaller batch sizes down to a single record), and because the gap is
    recorded in the output, not hidden.

    Returns (records, blocked_at_url_or_None)."""
    records = []
    url = (
        f"{API_BASE}/search/resources"
        f"?institution={INSTITUTION_ID}&publicationYear={year}"
        f"&size={PAGE_SIZE}&sort=identifier"
    )
    page = 0
    blocked_at = None
    while url and page < MAX_PAGES:
        page += 1
        try:
            data = fetch_json(url)
        except Exception:
            data = None
            for fallback_size in FALLBACK_PAGE_SIZES:
                fallback_url = re.sub(r"size=\d+", f"size={fallback_size}", url)
                print(f"    [year {year}] page failed at the default size; retrying at size={fallback_size}...", file=sys.stderr)
                try:
                    data = fetch_json(fallback_url)
                    print(f"    [year {year}] size={fallback_size} succeeded.", file=sys.stderr)
                    break
                except Exception:
                    continue
            if data is None:
                blocked_at = url
                print(
                    f"    [year {year}] even size=1 failed at this position — stopping this "
                    f"year's fetch here, keeping {len(records)} record(s) already fetched for {year}.",
                    file=sys.stderr,
                )
                break
        hits = data.get("hits", [])
        records.extend(hits)
        url = data.get("nextSearchAfterResults") or None
        if not hits:
            break
    if page >= MAX_PAGES:
        raise RuntimeError(
            f"Hit MAX_PAGES safety cap ({MAX_PAGES}) for year {year} — "
            f"pagination may be looping. Aborting rather than risk an infinite/partial run."
        )
    return records, blocked_at


def fetch_all_publications():
    """One query per year (YEAR_MIN..current+1) instead of one continuous whole-corpus
    search_after chain — see fetch_publications_for_year's docstring for why. Returns
    (records, gaps) — gaps is a list of {"year", "blocked_at_url", "records_fetched"}
    entries for any year where pagination had to stop early even after the fallback-size
    retries in fetch_publications_for_year, so main() can record them explicitly in the
    snapshot rather than the gap being invisible."""
    true_total = fetch_true_total_count()
    print(f"NVA's own reported total for institution={INSTITUTION_ID}: {true_total}")

    all_records = []
    gaps = []
    current_year = datetime.now(timezone.utc).year
    for year in range(YEAR_MIN, current_year + 2):
        year_records, blocked_at = fetch_publications_for_year(year)
        if year_records:
            print(f"  Year {year}: {len(year_records)} records (running total: {len(all_records) + len(year_records)})")
        if blocked_at:
            gaps.append({"year": year, "blocked_at_url": blocked_at, "records_fetched": len(year_records)})
        all_records.extend(year_records)

    print(f"Done: {len(all_records)} records fetched across years {YEAR_MIN}-{current_year + 1}.")
    if gaps:
        print(
            f"WARNING: {len(gaps)} year(s) hit a persistent server error partway through, even "
            f"at the smallest fallback size — pagination stopped early for: "
            f"{[g['year'] for g in gaps]}. Records fetched before each block are kept; see "
            f"'pagination_gaps' in the output snapshot for exact detail.",
            file=sys.stderr,
        )
    if isinstance(true_total, int) and len(all_records) != true_total and not gaps:
        # Only surfaced when there's no other explanation already logged above — if gaps
        # exist, the shortfall is already accounted for and repeating the warning would
        # just be noise on top of a more specific one.
        print(
            f"WARNING: fetched {len(all_records)} records but NVA reported {true_total} total, "
            f"with no pagination gaps recorded. Could mean YEAR_MIN={YEAR_MIN} is too narrow "
            f"(widen it), or that some records have no publication year and fall outside every "
            f"year-sliced query — worth investigating before trusting this snapshot.",
            file=sys.stderr,
        )
    return all_records, gaps


# ---- Record extraction --------------------------------------------------------

def extract_channel_ref_and_level(reference):
    """Returns (channel_ref_url, direct_level).

    Confirmed via a live diagnostic against 5 real USN journal articles (see
    conversation): for Journal-type contexts, publicationContext is ONLY {id, type} — a
    REFERENCE to a separate, year-versioned channel resource
    (publication-channels-v2/serial-publication/{uuid}/{year}) where scientificValue
    actually lives. The publication record itself never carries the level directly for
    this case. This matters a lot: Journal-type contexts cover journal articles, the
    single largest NVI-eligible category, so this reference has to be resolved (see
    resolve_channel_levels() and main()) for level/points to be meaningfully correct —
    checking only the publication record, as an earlier version of this script did,
    silently missed the level for nearly all journal articles.

    For Report/Publisher-type contexts, scientificValue CAN appear directly embedded on
    context.publisher (confirmed against NVA's own documented example) — that's returned
    directly here as a shortcut, so those records don't need a resolution fetch at all.
    Both checks run every time since context type isn't itself a reliable signal for
    which shape applies."""
    context = reference.get("publicationContext") or {}
    direct_level = context.get("scientificValue")
    if not direct_level:
        direct_level = (context.get("publisher") or {}).get("scientificValue")
    if not direct_level:
        direct_level = (context.get("series") or {}).get("scientificValue")
    if direct_level not in ("LevelOne", "LevelTwo"):
        direct_level = None
    channel_ref = context.get("id")
    return channel_ref, direct_level


def resolve_channel_levels(channel_refs):
    """Resolves scientificValue for each distinct referenced channel resource — one
    fetch per distinct (channel, year) combination, which is far fewer than one per
    publication, since many articles share the same journal in the same year. A single
    channel failing to resolve is a cosmetic/coverage gap for publications using it, not
    a structural problem — logged, not fatal, same reasoning as partner org resolution."""
    resolved = {}
    refs = sorted(r for r in channel_refs if r)
    print(f"Resolving {len(refs)} distinct channel-year reference(s) for journal-type levels...")
    for i, ref in enumerate(refs, 1):
        try:
            data = fetch_json(ref)
            level = data.get("scientificValue")
            resolved[ref] = level if level in ("LevelOne", "LevelTwo") else None
        except Exception as e:
            print(f"  [warn] Could not resolve channel {ref}: {e} — level will stay unknown for publications using it.", file=sys.stderr)
            resolved[ref] = None
        if i % 100 == 0:
            print(f"  ...resolved {i}/{len(refs)}")
    return resolved


_diagnostic_state = {"artifact_printed": False}


def has_open_file(raw):
    """No dedicated OA field in NVA's schema — inferred from whether at least
    one associated artifact is a publicly downloadable file.

    Confirmed against a real record (a first run returned an implausible 0.0%
    across 57,860 records, then a one-time diagnostic print showed the actual
    shape): the artifact type is "OpenFile", not "PublishedFile" as the one
    documentation example this was originally built from suggested, and there
    is no "visibleForNonOwner" field at all — both were silently excluding
    every single record.

    Deliberately does NOT also require a specific license: the same real
    example had an OpenFile whose license was "COPYRIGHT-ACT" ("General
    Terms of Use") — restrictive, not an open license — while still being a
    freely downloadable PDF. NVA's OpenFile type is tracking *accessibility*
    (can someone read this for free), not *reuse rights* (can they adapt or
    redistribute it) — that's the right thing for this flag to mean, matching
    the common "green OA" sense most institutional reporting cares about, but
    it is NOT the same claim as "openly licensed for reuse", and should be
    labeled that way wherever it's surfaced in the client tool."""
    artifacts = raw.get("associatedArtifacts", []) or []
    if artifacts and not _diagnostic_state["artifact_printed"]:
        _diagnostic_state["artifact_printed"] = True
        print(f"DIAGNOSTIC: first record with any associatedArtifacts (id={raw.get('id')}):", file=sys.stderr)
        print(f"  {json.dumps(artifacts, indent=2, ensure_ascii=False)[:2000]}", file=sys.stderr)
    for artifact in artifacts:
        if artifact.get("type") == "OpenFile":
            return True
    return False


def compute_author_shares(contributors):
    """Returns (N, n, partner_roots): N is total author-shares (every unique
    author x institution-root combination, per DBH's official definition —
    see module docstring), n is USN's own share of those, and partner_roots
    is the set of distinct non-USN institution roots involved (used both for
    the co-publishing breakdown and, once countries are resolved, the
    international-collaboration multiplier)."""
    total_pairs = 0
    usn_pairs = 0
    partner_roots = set()
    for contributor in contributors:
        roots = set()
        for affiliation in contributor.get("affiliations", []) or []:
            root = institution_root(affiliation.get("id"))
            if root:
                roots.add(root)
        total_pairs += len(roots)
        if INSTITUTION_ORG_ROOT in roots:
            usn_pairs += 1
        partner_roots |= (roots - {INSTITUTION_ORG_ROOT})
    return total_pairs, usn_pairs, partner_roots


def compute_base_nvi_points(category, level, n, total_pairs):
    """weight(form, level) x sqrt(n/N) — everything except the international
    multiplier, which needs resolved partner-country data not available yet
    at extraction time (see main()). Returns (points, form); points is 0.0
    (not None) for non-eligible categories/channels, since "not scientific"
    and "scientific but zero computed" both aggregate the same way."""
    form = NVI_FORM_BY_CATEGORY.get(category)
    if not form or level not in ("LevelOne", "LevelTwo") or total_pairs == 0 or n == 0:
        return 0.0, form
    weight = NVI_WEIGHTS[form][level]
    share = math.sqrt(n / total_pairs)
    return weight * share, form


def extract_person_id(identity_id):
    """Cristin person URIs end in a numeric ID (e.g. .../cristin/person/11111) — pulls
    just that trailing ID, the same trailing-segment convention already used for
    organization codes throughout this script."""
    if not identity_id:
        return None
    code = identity_id.rsplit("/", 1)[-1]
    return code or None


def extract_record(raw):
    """Pull out exactly the fields the client tool needs, plus the set of
    distinct department/faculty units this work should be counted under,
    channel level, a provisional NVI point value (international multiplier
    applied afterward in main(), once partner countries are resolved), an
    inferred open-access flag, and the USN-affiliated contributors on this
    work (for the "virtual research group" feature — see conversation).

    Returns (record, person_names): person_names is a small {person_id: name}
    dict for just this record's USN-affiliated contributors, merged into a
    single snapshot-wide `authors` dict in main() rather than repeating names
    on every publication — the same reference-by-code pattern already used
    for units/organizations and partner_orgs/partner_organizations. Only
    USN-affiliated contributors are tracked at all: external co-authors'
    names aren't needed for a USN research-group feature, and leaving them
    out keeps both the snapshot smaller and the eventual author search
    scoped to people actually worth building a USN group around."""
    entity = raw.get("entityDescription") or {}
    reference = entity.get("reference") or {}
    instance = reference.get("publicationInstance") or {}
    pub_date = entity.get("publicationDate") or {}
    contributors = entity.get("contributors", [])

    units = set()
    author_ids = set()
    person_names = {}
    for contributor in contributors:
        identity = contributor.get("identity") or {}
        person_id = extract_person_id(identity.get("id"))
        contributor_units = set()
        for affiliation in contributor.get("affiliations", []) or []:
            unit_id = affiliation.get("id")
            if unit_id:
                unit_code = unit_id.rsplit("/", 1)[-1]
                units.add(unit_code)
                contributor_units.add(unit_code)
        is_home_contributor = any(institution_root(u) == INSTITUTION_ORG_ROOT for u in contributor_units)
        if person_id and is_home_contributor:
            author_ids.add(person_id)
            if identity.get("name"):
                person_names[person_id] = identity["name"]

    year = pub_date.get("year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None

    category = instance.get("type")
    channel_ref, direct_level = extract_channel_ref_and_level(reference)
    total_pairs, usn_pairs, partner_roots = compute_author_shares(contributors)

    record = {
        "id": raw.get("id"),
        "identifier": raw.get("identifier"),
        "title": entity.get("mainTitle"),
        "year": year,
        "type": category,
        "doi": reference.get("doi"),
        "units": sorted(units),
        "level": direct_level,            # finalized in main() once channel_ref (if any) is resolved
        "nvi_form": NVI_FORM_BY_CATEGORY.get(category),
        "points": 0.0,                    # finalized in main() — needs level to be final first
        "international": False,           # finalized in main(), once partner countries are known
        "partner_orgs": sorted(partner_roots),
        "open_access": has_open_file(raw),
        "author_ids": sorted(author_ids),
        "_channel_ref": channel_ref,      # internal only — resolved and stripped in main(), never written to the output file
        "_usn_pairs": usn_pairs,          # internal only — needed to compute points once level is final
        "_total_pairs": total_pairs,      # internal only — needed to compute points once level is final
    }
    return record, person_names


# ---- Main ------------------------------------------------------------------

def main():
    org_tree_raw = fetch_org_tree()
    org_flat = flatten_org_tree(org_tree_raw)
    print(f"Flattened {len(org_flat)} organizational units under USN.")

    raw_records, pagination_gaps = fetch_all_publications()
    print(f"Fetched {len(raw_records)} raw publication records.")

    publications = []
    all_person_names = {}
    for r in raw_records:
        record, person_names = extract_record(r)
        publications.append(record)
        all_person_names.update(person_names)

    # ---- Resolve journal-type channel levels (see extract_channel_ref_and_level's
    # docstring — confirmed via live diagnostic that Journal-type contexts only carry a
    # reference to a separate channel resource, not the level itself) ----
    channel_refs_needed = set(p["_channel_ref"] for p in publications if p["_channel_ref"] and not p["level"])
    channel_levels_resolved = resolve_channel_levels(channel_refs_needed)
    for p in publications:
        if not p["level"] and p["_channel_ref"]:
            p["level"] = channel_levels_resolved.get(p["_channel_ref"])

    # Points can only be computed correctly now that level is actually final — this is
    # why extract_record() no longer computes points itself.
    for p in publications:
        base_points, _form = compute_base_nvi_points(p["type"], p["level"], p["_usn_pairs"], p["_total_pairs"])
        p["points"] = base_points

    # ---- Resolve co-publishing partners + finalize the international flag/points ----
    all_partner_roots = set()
    for p in publications:
        all_partner_roots |= set(p["partner_orgs"])
    partner_orgs_resolved = resolve_partner_organizations(all_partner_roots)

    for p in publications:
        is_foreign = any(
            partner_orgs_resolved.get(code, {}).get("country_code") not in (None, "NO")
            for code in p["partner_orgs"]
        )
        p["international"] = is_foreign
        if is_foreign and p["points"] > 0:
            p["points"] = round(p["points"] * 1.3, 6)
        else:
            p["points"] = round(p["points"], 6)
        # Internal-only fields, never meant for the output file — strip them here now
        # that everything that needed them (points, above) has already run.
        del p["_channel_ref"]
        del p["_usn_pairs"]
        del p["_total_pairs"]

    # Sanity checks — surfaced loudly, not silently absorbed. A snapshot that
    # looks structurally fine but has, say, near-zero DOI coverage or a pile
    # of untagged units is a sign something about the extraction broke.
    with_doi = sum(1 for p in publications if p["doi"])
    with_no_unit = sum(1 for p in publications if not p["units"])
    with_level = sum(1 for p in publications if p["level"])
    with_points = sum(1 for p in publications if p["points"] > 0)
    with_oa = sum(1 for p in publications if p["open_access"])
    international_count = sum(1 for p in publications if p["international"])
    with_no_author_id = sum(1 for p in publications if p["units"] and not p["author_ids"])
    by_type = {}
    by_nvi_form = {}
    for p in publications:
        by_type[p["type"] or "Unknown"] = by_type.get(p["type"] or "Unknown", 0) + 1
        by_nvi_form[p["nvi_form"] or "(not NVI-eligible)"] = by_nvi_form.get(p["nvi_form"] or "(not NVI-eligible)", 0) + 1

    print(f"DOI coverage: {with_doi}/{len(publications)} ({100*with_doi/len(publications):.1f}%)")
    print(f"Records with NO recognized unit tag: {with_no_unit}")
    print(f"Channel level coverage: {with_level}/{len(publications)} ({100*with_level/len(publications):.1f}%)")
    print(f"Records with computed points > 0: {with_points}/{len(publications)} ({100*with_points/len(publications):.1f}%)")
    print(f"Inferred open access: {with_oa}/{len(publications)} ({100*with_oa/len(publications):.1f}%)")
    print(f"International (foreign co-affiliation): {international_count}/{len(publications)}")
    print(f"Distinct USN authors identified: {len(all_person_names)}")
    print(f"Records with a USN unit but NO resolved author ID (contributor entry with no identity.id): {with_no_author_id}")
    print("Breakdown by NVA category (type):")
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {t}: {n}")
    print("Breakdown by NVI form (after category mapping — spot-check this):")
    for f, n in sorted(by_nvi_form.items(), key=lambda kv: -kv[1]):
        print(f"  {f}: {n}")

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

    # Same precomputed-index pattern as by_unit — author_id -> [publication indices] —
    # so the browser tool doesn't have to scan every publication per selected person.
    by_author = {}
    for idx, p in enumerate(publications):
        for author_id in p["author_ids"]:
            by_author.setdefault(author_id, []).append(idx)

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": f"NVA (api.nva.unit.no/search/resources), institution={INSTITUTION_ID}",
        "institution_id": INSTITUTION_ID,
        "institution_total_publications": len(publications),
        "doi_coverage": {"with_doi": with_doi, "total": len(publications)},
        "organizations": org_flat,
        "partner_organizations": partner_orgs_resolved,
        # {person_id: name} for every USN-affiliated contributor encountered — external
        # co-authors aren't tracked here (see extract_record's docstring for why).
        "authors": all_person_names,
        "publications": publications,
        "by_unit": by_unit,
        "by_author": by_author,
        # Any year(s) where a persistent NVA-side server error blocked pagination even at
        # the smallest fallback size (see fetch_publications_for_year) — empty in the
        # normal case. Present explicitly, not just as a log line, so the gap is visible
        # to anyone reading the snapshot itself, not only whoever happened to read this
        # run's Action log.
        "pagination_gaps": pagination_gaps,
    }

    if pagination_gaps:
        print(
            f"NOTE: this snapshot has {len(pagination_gaps)} known pagination gap(s) "
            f"(see 'pagination_gaps' in the output) — years affected: "
            f"{[g['year'] for g in pagination_gaps]}.",
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=None, separators=(",", ":")))
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1024 / 1024:.2f} MB)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
