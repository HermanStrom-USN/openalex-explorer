#!/usr/bin/env python3
"""
Converts a manually-exported DBH "Publiseringspoeng per faglig årsverk" Excel file into a
compact JSON snapshot the app can load, the same way build_nva_snapshot.py converts NVA's
API responses. Unlike that script, this one has no live API to call — DBH's publication/
staffing statistics portal (https://dbh.hkdir.no/tall-og-statistikk/statistikk-meny/publisering)
doesn't expose this pivot programmatically as far as we've confirmed, so the workflow is:
Herman re-exports the Excel from DBH's portal once a year (the source report notes "DBH i
mai 2026" as the timing for last year's pull), then re-runs this script locally and commits
the resulting JSON — no GitHub Action, no schedule, just a manual step alongside the report.

Why this data matters and why it's kept separate from build_nva_snapshot.py's own computed
`points` field: this app's own NVI point formula has an unconfirmed category-mapping
assumption (see build_nva_snapshot.py's own docstring) and works at the individual-publication
level. DBH's own figures are the actual authoritative numbers the published report uses —
confirmed by cross-checking USN's 2025 points/UFF-årsverk from this exact file
(926.28 / 1111.24 = 0.8336, rounding to the report's stated 0.83). For cross-institution and
cross-faculty comparison specifically, DBH's own numbers are what should be shown, not our own
approximation — our own per-publication `points` field remains the right tool for
per-publication or per-research-group views, where DBH has no equivalent at all (DBH's data
here is pre-aggregated at institution/faculty level, nothing finer).

Also worth knowing: DBH's own "Universiteter" category has 11 institutions, not the 10 the
app's existing NORWAY_UNIVERSITIES/CRISTIN_UNIVERSITIES lists use — it includes both Nord
universitet AND Universitetet i Innlandet (INN). The app's lists made an unexplained choice
to include Nord over INN; this script surfaces both, but only the 10 with a confirmed Cristin
institution code (see CRISTIN_CODE_BY_DBH_NAME below) get a usable cristin_code field for
joining against NVA-derived data — INN's Cristin code was never verified in this project (see
conversation), so it appears in the output but without one, clearly distinguishable rather
than silently guessed.

USN's own faculty-level rows include a handful of small administrative units (library
services, quality office, HR) with near-zero UFF-årsverk that the published report's own
faculty charts clearly exclude — this script keeps them in the output rather than silently
dropping them (same "no silent data loss" principle as build_nva_snapshot.py), flagged via
is_small_admin_unit so the app's display layer can filter them, not this conversion step.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

SOURCE_XLSX = Path("publiseringspoeng_per_uff.xlsx")  # update this path per year's export
OUTPUT_PATH = Path("data/dbh-points-per-uff-snapshot.json")

USN_DBH_NAME = "Universitetet i Sørøst-Norge"

# Confirmed via the live diagnostic tool used earlier in this project (real Cristin API
# responses, not guessed) — see conversation. INN is deliberately absent: its code was
# never verified, so it's left unmapped rather than guessed.
CRISTIN_CODE_BY_DBH_NAME = {
    "Universitetet i Sørøst-Norge": "222",
    "Universitetet i Oslo": "185",
    "Norges teknisk-naturvitenskapelige universitet": "194",
    "Universitetet i Bergen": "184",
    "Universitetet i Tromsø - Norges arktiske universitet": "186",
    "Norges miljø- og biovitenskapelige universitet": "192",
    "Universitetet i Agder": "201",
    "Universitetet i Stavanger": "217",
    "OsloMet – storbyuniversitetet": "215",
    "Nord universitet": "204",
    # "Universitetet i Innlandet": intentionally omitted — no confirmed Cristin code.
}

# Small administrative/support units at USN that aren't one of the four real research
# faculties — near-zero UFF-årsverk, and the published report's own faculty-level charts
# don't include them. Matched by substring since exact names vary slightly year to year
# (e.g. "Avdeling for..." naming has shifted before).
ADMIN_UNIT_NAME_PATTERNS = [
    "avdeling for forskning", "avdeling for utdanning", "enhet for analyse",
    "personal- og organisasjon", "uspesifisert underenhet",
]


def is_small_admin_unit(fakultet_name):
    name_lower = str(fakultet_name).lower()
    return any(pattern in name_lower for pattern in ADMIN_UNIT_NAME_PATTERNS)


def main():
    if not SOURCE_XLSX.exists():
        raise RuntimeError(
            f"{SOURCE_XLSX} not found. Export the current year's data from DBH's "
            f"publication statistics portal (https://dbh.hkdir.no/tall-og-statistikk/"
            f"statistikk-meny/publisering) and place it at this path before running."
        )

    df = pd.read_excel(SOURCE_XLSX, sheet_name="Data")
    required_cols = {"Årstall", "Institusjonskategori", "Institusjon", "Fakultet",
                      "Publiseringspoeng", "Poeng, nivå 2", "UFF-årsverk"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        raise RuntimeError(
            f"Expected columns missing from the source file: {missing_cols}. "
            f"DBH may have changed their export format — check before trusting this output."
        )

    # ---- Cross-university comparison: one row per (institution, year) ----
    uni_df = df[df["Institusjonskategori"] == "Universiteter"].copy()
    uni_grouped = uni_df.groupby(["Institusjon", "Årstall"], as_index=False).agg({
        "Publiseringspoeng": "sum", "Poeng, nivå 2": "sum", "UFF-årsverk": "sum",
    })

    universities = {}
    for dbh_name, sub in uni_grouped.groupby("Institusjon"):
        cristin_code = CRISTIN_CODE_BY_DBH_NAME.get(dbh_name)
        by_year = {}
        for _, row in sub.iterrows():
            year = int(row["Årstall"])
            points = float(row["Publiseringspoeng"]) if pd.notna(row["Publiseringspoeng"]) else 0.0
            uff = float(row["UFF-årsverk"]) if pd.notna(row["UFF-årsverk"]) else 0.0
            by_year[str(year)] = {
                "points": round(points, 4),
                "points_level2": round(float(row["Poeng, nivå 2"]) if pd.notna(row["Poeng, nivå 2"]) else 0.0, 4),
                "uff": round(uff, 4),
                "points_per_uff": round(points / uff, 4) if uff > 0 else None,
            }
        universities[dbh_name] = {
            "dbh_name": dbh_name,
            "cristin_code": cristin_code,  # None for institutions with no confirmed code (e.g. INN)
            "by_year": by_year,
        }

    # ---- USN's own faculty-level breakdown: one row per (fakultet, year) ----
    usn_df = df[df["Institusjon"] == USN_DBH_NAME].copy()
    usn_grouped = usn_df.groupby(["Fakultet", "Årstall"], as_index=False).agg({
        "Publiseringspoeng": "sum", "Poeng, nivå 2": "sum", "UFF-årsverk": "sum",
    })

    usn_faculties = {}
    for fakultet_name, sub in usn_grouped.groupby("Fakultet"):
        by_year = {}
        for _, row in sub.iterrows():
            year = int(row["Årstall"])
            points = float(row["Publiseringspoeng"]) if pd.notna(row["Publiseringspoeng"]) else 0.0
            uff = float(row["UFF-årsverk"]) if pd.notna(row["UFF-årsverk"]) else 0.0
            by_year[str(year)] = {
                "points": round(points, 4),
                "points_level2": round(float(row["Poeng, nivå 2"]) if pd.notna(row["Poeng, nivå 2"]) else 0.0, 4),
                "uff": round(uff, 4),
                "points_per_uff": round(points / uff, 4) if uff > 0 else None,
            }
        usn_faculties[fakultet_name] = {
            "is_small_admin_unit": is_small_admin_unit(fakultet_name),
            "by_year": by_year,
        }

    # Sanity check, loud not silent — same principle as build_nva_snapshot.py. Cross-check
    # against the one figure we know is correct from the published report: USN 2025 should
    # be 0.83 points/UFF (0.8336 unrounded, confirmed against this exact file already).
    usn_2025 = universities.get(USN_DBH_NAME, {}).get("by_year", {}).get("2025")
    if usn_2025 and usn_2025["points_per_uff"] is not None:
        rounded = round(usn_2025["points_per_uff"], 2)
        print(f"USN 2025 points/UFF: {usn_2025['points_per_uff']} (rounds to {rounded}, report states 0.83)")
        if rounded != 0.83:
            print(
                f"WARNING: expected 0.83 for USN 2025 based on the published report — got {rounded}. "
                f"Something about this export may differ from what the report used (a later DUCT "
                f"correction, a different snapshot date, etc.) — worth checking before trusting this "
                f"output.",
                file=sys.stderr,
            )
    else:
        print("WARNING: USN 2025 data not found in this export — check the source file covers the expected year.", file=sys.stderr)

    unmapped = [name for name, u in universities.items() if u["cristin_code"] is None]
    if unmapped:
        print(f"NOTE: {len(unmapped)} institution(s) have no confirmed Cristin code and won't join against NVA-derived data: {unmapped}")

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "DBH (dbh.hkdir.no), manually exported — see module docstring for the portal URL",
        "universities": universities,
        "usn_faculties": usn_faculties,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=None, separators=(",", ":")))
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
