"""Build a crosswalk between two Future Land Use (FLU) vintages.

The script matches zones from a new FLU vintage to the previous vintage using
three passes (exact -> normalized -> fuzzy name match), then fills any
remaining gaps via spatial overlap. Finally, any manual overrides recorded in
a working Excel file are applied. See old_flu_crosswalk.md for usage.
"""

import re
from difflib import SequenceMatcher
from pathlib import Path

import geopandas as gpd
import pandas as pd

from future_land_use.util.pipeline import Pipeline


# Jurisdiction alias mapping: normalized form -> canonical normalized form.
# Used so the same jurisdiction under different spellings groups together.
# Note: "snohomish" (city) is intentionally NOT mapped to "snohomishcounty".
JURISDICTION_ALIASES = {
    "mlt": "mountlaketerrace",
    "bainbridge": "bainbridgeisland",
    "mercer": "mercerisland",
    "up": "universityplace",
    "snoco": "snohomishcounty",
    "pierce": "piercecounty",
    "kitsap": "kitsapcounty",
    "king": "kingcounty",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_zone(s) -> str:
    """Normalize a zone string for near-matching: lowercase, strip, collapse separators."""
    s = str(s).strip().lower()
    return re.sub(r"[-_\s,/]+", "", s)


def normalize_jurisdiction(s) -> str:
    """Normalize a jurisdiction name and apply known aliases."""
    norm = normalize_zone(s)
    return JURISDICTION_ALIASES.get(norm, norm)


# ---------------------------------------------------------------------------
# Load source data
# ---------------------------------------------------------------------------

def load_sources(
    flu_table: pd.DataFrame,
    old_flu_shp: gpd.GeoDataFrame,
    old_xwalk: pd.DataFrame,
    new_flu_shp: gpd.GeoDataFrame,
    new_yr,
    old_yr,
):
    """Standardize the new-vintage XLSX, old-vintage shapefile, and old
    description crosswalk.  Returned DataFrames use fixed column names so
    downstream functions don't need to know the vintage years."""

    flu_table = flu_table.drop(
        columns=[c for c in flu_table.columns if "Unnamed" in c]
    ).add_suffix(f"_{new_yr}")

    old_flu = old_flu_shp.merge(
        old_xwalk, on="juris_zn", how="left"
    )
    old_flu_table = old_flu.drop(columns=["geometry"]).copy().add_suffix(f"_{old_yr}")
    old_flu_shp_out = old_flu[["juris_zn", "definition", "geometry"]].copy()

    flu_shp = new_flu_shp[["juris_zn", "geometry"]].copy()

    return flu_table, old_flu_table, old_flu_shp_out, flu_shp


# ---------------------------------------------------------------------------
# Build unique per-jurisdiction zone lists
# ---------------------------------------------------------------------------

def build_zone_lists(
    flu_table,
    old_flu_table,
    new_juris_col,
    new_zone_col,
    new_desc_col,
    old_juris_col,
    old_zone_col,
    old_desc_col,
    zone_new,
    zone_old,
    desc_new,
    desc_old,
):
    old_zones = (
        old_flu_table[[old_juris_col, old_zone_col, old_desc_col]]
        .drop_duplicates()
        .rename(columns={
            old_juris_col: "jurisdiction",
            old_zone_col: zone_old,
            old_desc_col: desc_old,
        })
    )

    new_zones = (
        flu_table[[new_juris_col, new_zone_col, new_desc_col]]
        .drop_duplicates()
        .rename(columns={
            new_juris_col: "jurisdiction",
            new_zone_col: zone_new,
            new_desc_col: desc_new,
        })
        .drop_duplicates(subset=["jurisdiction", zone_new], keep="first")
    )

    print(f"Old zones: {len(old_zones)} unique rows")
    print(f"New zones: {len(new_zones)} unique rows")
    return old_zones, new_zones


# ---------------------------------------------------------------------------
# Name-based crosswalk (exact -> normalized -> fuzzy)
# ---------------------------------------------------------------------------

def build_name_crosswalk(
    new_zones: pd.DataFrame,
    old_zones: pd.DataFrame,
    zone_new: str,
    zone_old: str,
    desc_new: str,
    desc_old: str,
    fuzzy_match_cutoff: float = 0.4,
) -> pd.DataFrame:
    """Match new zones to old zones by name, within each jurisdiction."""
    new_zones = new_zones.copy()
    old_zones = old_zones.copy()
    new_zones["jurisdiction_norm"] = new_zones["jurisdiction"].apply(normalize_jurisdiction)
    old_zones["jurisdiction_norm"] = old_zones["jurisdiction"].apply(normalize_jurisdiction)

    rows = []

    for juris_norm in new_zones["jurisdiction_norm"].unique():
        new_j = new_zones[new_zones["jurisdiction_norm"] == juris_norm]
        old_j = old_zones[old_zones["jurisdiction_norm"] == juris_norm]
        juris = new_j["jurisdiction"].iloc[0]  # canonical new-vintage name

        matched_old: set = set()
        matched_new: set = set()

        # Pass 1: exact match on zone name
        for ni, nrow in new_j.iterrows():
            for oi, orow in old_j.iterrows():
                if oi in matched_old:
                    continue
                if str(nrow[zone_new]).strip() == str(orow[zone_old]).strip():
                    rows.append({
                        "jurisdiction": juris,
                        zone_old: orow[zone_old], desc_old: orow[desc_old],
                        zone_new: nrow[zone_new], desc_new: nrow[desc_new],
                        "match_type": "exact", "confidence": 1.0,
                    })
                    matched_old.add(oi)
                    matched_new.add(ni)
                    break

        # Pass 2: normalized near-match on zone name
        remaining_new = new_j[~new_j.index.isin(matched_new)]
        remaining_old = old_j[~old_j.index.isin(matched_old)]
        for ni, nrow in remaining_new.iterrows():
            norm_new = normalize_zone(nrow[zone_new])
            for oi, orow in remaining_old.iterrows():
                if oi in matched_old:
                    continue
                if norm_new == normalize_zone(orow[zone_old]):
                    rows.append({
                        "jurisdiction": juris,
                        zone_old: orow[zone_old], desc_old: orow[desc_old],
                        zone_new: nrow[zone_new], desc_new: nrow[desc_new],
                        "match_type": "near_match", "confidence": 0.8,
                    })
                    matched_old.add(oi)
                    matched_new.add(ni)
                    break

        # Pass 3: fuzzy match for any still-unmatched new zones
        remaining_new = new_j[~new_j.index.isin(matched_new)]
        remaining_old = old_j[~old_j.index.isin(matched_old)]
        for ni, nrow in remaining_new.iterrows():
            best_score = 0.0
            best_oi = None
            best_orow = None
            for oi, orow in remaining_old.iterrows():
                if oi in matched_old:
                    continue
                zone_sim = SequenceMatcher(
                    None,
                    normalize_zone(nrow[zone_new]),
                    normalize_zone(orow[zone_old]),
                ).ratio()
                if zone_sim > best_score:
                    best_score = zone_sim
                    best_oi = oi
                    best_orow = orow

            if best_score >= fuzzy_match_cutoff and best_orow is not None:
                rows.append({
                    "jurisdiction": juris,
                    zone_old: best_orow[zone_old], desc_old: best_orow[desc_old],
                    zone_new: nrow[zone_new], desc_new: nrow[desc_new],
                    "match_type": "fuzzy", "confidence": round(best_score, 3),
                })
                matched_old.add(best_oi)
            else:
                # New zone with no old match -- still included for completeness
                rows.append({
                    "jurisdiction": juris,
                    zone_old: None, desc_old: None,
                    zone_new: nrow[zone_new], desc_new: nrow[desc_new],
                    "match_type": "new_zone", "confidence": 0.0,
                })

    crosswalk = pd.DataFrame(rows)
    print(crosswalk["match_type"].value_counts())
    print(f"\nTotal crosswalk rows: {len(crosswalk)}")
    print(
        f"Total new zones: {len(new_zones)} -- all accounted for: "
        f"{len(crosswalk) == len(new_zones)}"
    )
    return crosswalk


def annotate_review_columns(crosswalk: pd.DataFrame) -> pd.DataFrame:
    """Add needs_review / manual_match / confirmed_new helper columns."""
    crosswalk = crosswalk.copy()
    crosswalk["needs_review"] = crosswalk["match_type"].isin(["fuzzy", "new_zone"])
    crosswalk["manual_match"] = ""
    crosswalk["confirmed_new"] = False

    print("=== Match Summary ===")
    print(
        crosswalk.groupby("match_type")["confidence"]
        .describe()[["count", "mean", "min", "max"]]
    )
    print(f"\nRows needing manual review: {crosswalk['needs_review'].sum()}")
    print(f"Rows auto-matched (exact + near): {(~crosswalk['needs_review']).sum()}")
    return crosswalk


# ---------------------------------------------------------------------------
# Spatial crosswalk (area-overlap match)
# ---------------------------------------------------------------------------

def build_spatial_xwalk(
    flu_shp: gpd.GeoDataFrame,
    old_flu_shp: gpd.GeoDataFrame,
    spatial_new_key: str,
    spatial_old_key: str,
    spatial_overlap_cutoff: float = 0.9,
) -> pd.DataFrame:
    """For each new zone, find the old zone with the greatest area overlap."""
    old_aligned = (
        old_flu_shp.to_crs(flu_shp.crs)
        if flu_shp.crs != old_flu_shp.crs
        else old_flu_shp
    )

    new_zones_geo = (
        flu_shp.dissolve(by="juris_zn").reset_index()[["juris_zn", "geometry"]]
        .rename(columns={"juris_zn": spatial_new_key})
    )
    old_zones_geo = (
        old_aligned.dissolve(by="juris_zn").reset_index()[["juris_zn", "geometry"]]
        .rename(columns={"juris_zn": spatial_old_key})
    )

    new_zones_geo["new_area"] = new_zones_geo.geometry.area

    overlay = gpd.overlay(
        new_zones_geo, old_zones_geo, how="intersection", keep_geom_type=True
    )
    overlay["intersection_area"] = overlay.geometry.area
    overlay["pct_overlap"] = overlay["intersection_area"] / overlay["new_area"]

    spatial_xwalk = (
        overlay
        .sort_values("pct_overlap", ascending=False)
        .drop_duplicates(subset=spatial_new_key, keep="first")
        [[spatial_new_key, spatial_old_key, "pct_overlap"]]
        .sort_values("pct_overlap")
        .reset_index(drop=True)
    )

    spatial_xwalk["likely_match"] = spatial_xwalk["pct_overlap"] >= spatial_overlap_cutoff

    print(f"Total new zones: {len(spatial_xwalk)}")
    print(
        f"Matches above {spatial_overlap_cutoff:.0%} cutoff: "
        f"{spatial_xwalk['likely_match'].sum()}"
    )
    print(f"Below cutoff (needs review): {(~spatial_xwalk['likely_match']).sum()}")
    return spatial_xwalk


def merge_spatial_and_fill(
    crosswalk: pd.DataFrame,
    spatial_xwalk: pd.DataFrame,
    old_zones: pd.DataFrame,
    zone_new: str,
    zone_old: str,
    desc_old: str,
    spatial_new_key: str,
    spatial_old_key: str,
    spatial_overlap_cutoff: float = 0.9,
) -> pd.DataFrame:
    """Merge spatial results onto the crosswalk and use them to fill unmatched rows."""
    spatial_suffixed = spatial_xwalk.add_suffix("_spatial")
    spatial_new_key_col = f"{spatial_new_key}_spatial"
    spatial_old_key_col = f"{spatial_old_key}_spatial"
    pct_overlap_col = "pct_overlap_spatial"

    crosswalk = crosswalk.merge(
        spatial_suffixed,
        left_on=zone_new,
        right_on=spatial_new_key_col,
        how="left",
    )

    unmatched = (
        crosswalk["needs_review"]
        & ~crosswalk["confirmed_new"]
        & crosswalk[spatial_old_key_col].notna()
    )

    for idx in crosswalk.loc[unmatched].index:
        spatial_zone = crosswalk.loc[idx, spatial_old_key_col]
        old_match = old_zones[old_zones[zone_old] == spatial_zone]
        if old_match.empty:
            continue
        pct = crosswalk.loc[idx, pct_overlap_col]
        crosswalk.loc[idx, zone_old] = spatial_zone
        crosswalk.loc[idx, desc_old] = old_match.iloc[0][desc_old]
        crosswalk.loc[idx, "match_type"] = "spatial"
        crosswalk.loc[idx, "confidence"] = pct
        crosswalk.loc[idx, "needs_review"] = pct < spatial_overlap_cutoff

    spatial_matched = (crosswalk["match_type"] == "spatial").sum()
    print(f"Spatially matched: {spatial_matched}")
    print(f"Rows still needing review: {crosswalk['needs_review'].sum()}")
    print(crosswalk["match_type"].value_counts())
    return crosswalk


# ---------------------------------------------------------------------------
# Manual overrides from working Excel file
# ---------------------------------------------------------------------------

def apply_manual_overrides(
    crosswalk: pd.DataFrame,
    old_zones: pd.DataFrame,
    working_xlsx: Path,
    zone_new: str,
    zone_old: str,
    desc_old: str,
) -> pd.DataFrame:
    """Apply confirmed_new flags and manual_match overrides from the working Excel.

    Does nothing (with a notice) if the working Excel doesn't exist yet, so the
    first run can proceed and produce the initial working file.
    """
    if not working_xlsx.exists():
        print(f"No existing {working_xlsx.name} found -- skipping manual override step.")
        return crosswalk

    manual = pd.read_excel(working_xlsx)

    # Backup before applying edits
    now = pd.Timestamp.now().strftime("%Y%m%d%H%M%S")
    backup_path = working_xlsx.with_name(f"{working_xlsx.stem}_backup_{now}.xlsx")
    manual.to_excel(backup_path, index=False)

    # Apply confirmed_new flags
    if "confirmed_new" in manual.columns:
        confirmed = manual[
            manual["confirmed_new"].astype(str).str.strip().str.upper()
            .isin(["TRUE", "1", "YES"])
        ]
        for _, row in confirmed.iterrows():
            mask = (
                (crosswalk["jurisdiction"] == row["jurisdiction"])
                & (crosswalk[zone_new] == row[zone_new])
            )
            if mask.any():
                crosswalk.loc[mask, "confirmed_new"] = True
                crosswalk.loc[mask, "needs_review"] = False
                print(f"  Confirmed new: {row['jurisdiction']} / {row[zone_new]}")
        print(f"Applied {len(confirmed)} confirmed_new flags")

    # Apply manual_match overrides
    manual_edits = manual[
        manual["manual_match"].notna()
        & (manual["manual_match"].astype(str).str.strip() != "")
    ]
    print(f"Found {len(manual_edits)} manual adjustments")

    for _, row in manual_edits.iterrows():
        mask = (
            (crosswalk["jurisdiction"] == row["jurisdiction"])
            & (crosswalk[zone_new] == row[zone_new])
        )
        if not mask.any():
            print(f"  WARNING: No match in crosswalk for {row['jurisdiction']} / {row[zone_new]}")
            continue

        manual_val = str(row["manual_match"]).strip()
        crosswalk.loc[mask, "manual_match"] = manual_val
        old_match = old_zones[old_zones[zone_old] == manual_val]
        if not old_match.empty:
            crosswalk.loc[mask, zone_old] = manual_val
            crosswalk.loc[mask, desc_old] = old_match.iloc[0][desc_old]
            crosswalk.loc[mask, "match_type"] = "manual"
            crosswalk.loc[mask, "confidence"] = 1.0
            crosswalk.loc[mask, "needs_review"] = False
        print(f"  Updated: {row['jurisdiction']} / {row[zone_new]} -> {manual_val}")

    print("\n=== Updated Match Summary ===")
    print(crosswalk["match_type"].value_counts())
    print(f"Rows still needing review: {crosswalk['needs_review'].sum()}")

    return crosswalk


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_working_files(
    crosswalk: pd.DataFrame,
    old_zones: pd.DataFrame,
    working_xlsx: Path,
    old_zones_xlsx: Path,
) -> None:
    crosswalk.to_excel(working_xlsx, index=False)
    old_zones.to_excel(old_zones_xlsx, index=False)
    print(f"Saved {working_xlsx.name} and {old_zones_xlsx.name}")


def write_final_master(
    crosswalk: pd.DataFrame,
    final_master_xlsx: Path,
    final_master_sheet: str,
    zone_new: str,
    zone_old: str,
    desc_new: str,
    desc_old: str,
    new_yr: str,
    old_yr: str,
) -> None:
    """Write the final, cleaned-up master corres file with standardized column names."""
    final = crosswalk.copy()
    final["FLU_master_id"] = final.index + 1
    juris_zn_new = f"juris_zn_{new_yr}"
    juris_zn_old = f"juris_zn_{old_yr}"
    final = final.rename(columns={zone_new: juris_zn_new, zone_old: juris_zn_old})
    out_cols = [
        "FLU_master_id",
        "jurisdiction",
        juris_zn_new,
        desc_new,
        juris_zn_old,
        desc_old,
    ]
    final[out_cols].to_excel(
        final_master_xlsx, index=False, sheet_name=final_master_sheet
    )
    print(f"Saved {final_master_xlsx.name}")

def run_step(context):
    p = Pipeline(settings_path=context['configs_dir'])
    global_cfg = p.settings
    cfg = global_cfg.get('old_flu_crosswalk_settings', {})

    ROOT_DIR = Path(global_cfg['root_dir'])

    # Years
    CURRENT_FLU_YEAR = cfg.get('current_flu_year', 2026)
    OLD_FLU_YEAR = cfg.get('old_flu_year', 2019)
    NEW_YR = str(CURRENT_FLU_YEAR)[-2:]
    OLD_YR = str(OLD_FLU_YEAR)[-2:]

    # Input paths
    new_flu_shp = p.get_geodataframe('flu_shp')
    flu_table_raw = p.get_table('flu_table')
    old_flu_shp_raw = p.get_geodataframe('old_flu_shp')
    old_xwalk = p.get_table('old_flu_crosswalk')

    # Output paths
    OUTPUT_DIR = ROOT_DIR / cfg.get('crosswalk_output_dir', 'old_flu_crosswalk')
    DATA_DIR = OUTPUT_DIR / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORKING_XLSX = DATA_DIR / f"flu_crosswalk_{OLD_YR}_to_{NEW_YR}_working.xlsx"
    OLD_ZONES_XLSX = DATA_DIR / f"old_zones_{OLD_YR}.xlsx"
    TODAY = pd.Timestamp.now().strftime("%Y-%m-%d")
    FINAL_MASTER_XLSX = OUTPUT_DIR / f"Full_FLU_Master_Corres_File_{TODAY}.xlsx"
    FINAL_MASTER_SHEET = "Full FLU Master Corres File"

    # Matching parameters
    FUZZY_MATCH_CUTOFF = cfg.get('fuzzy_match_cutoff', 0.4)
    SPATIAL_OVERLAP_CUTOFF = cfg.get('spatial_overlap_cutoff', 0.9)

    # Vintage-specific source column names (after add_suffix)
    NEW_JURIS_COL = f"juris_{NEW_YR}"
    NEW_ZONE_COL = f"juris_zn_{NEW_YR}"
    NEW_DESC_COL = f"definition_{NEW_YR}"
    OLD_JURIS_COL = f"juris_{OLD_YR}"
    OLD_ZONE_COL = f"juris_zn_{OLD_YR}"
    OLD_DESC_COL = f"definition_{OLD_YR}"

    # Unified short column names used inside the crosswalk DataFrame
    ZONE_NEW = f"zone_{NEW_YR}"
    ZONE_OLD = f"zone_{OLD_YR}"
    DESC_NEW = f"desc_{NEW_YR}"
    DESC_OLD = f"desc_{OLD_YR}"

    # Spatial overlay keys (kept separate from ZONE_NEW/OLD)
    SPATIAL_NEW_KEY = f"juris_zn_{NEW_YR}"
    SPATIAL_OLD_KEY = f"juris_zn_{OLD_YR}"

    # ---- pipeline ----
    flu_table, old_flu_table, old_flu_shp, flu_shp = load_sources(
        flu_table=flu_table_raw,
        old_flu_shp=old_flu_shp_raw,
        old_xwalk=old_xwalk,
        new_flu_shp=new_flu_shp,
        new_yr=NEW_YR,
        old_yr=OLD_YR,
    )

    old_zones, new_zones = build_zone_lists(
        flu_table, old_flu_table,
        new_juris_col=NEW_JURIS_COL,
        new_zone_col=NEW_ZONE_COL,
        new_desc_col=NEW_DESC_COL,
        old_juris_col=OLD_JURIS_COL,
        old_zone_col=OLD_ZONE_COL,
        old_desc_col=OLD_DESC_COL,
        zone_new=ZONE_NEW,
        zone_old=ZONE_OLD,
        desc_new=DESC_NEW,
        desc_old=DESC_OLD,
    )

    crosswalk = build_name_crosswalk(
        new_zones, old_zones,
        zone_new=ZONE_NEW,
        zone_old=ZONE_OLD,
        desc_new=DESC_NEW,
        desc_old=DESC_OLD,
        fuzzy_match_cutoff=FUZZY_MATCH_CUTOFF,
    )
    crosswalk = annotate_review_columns(crosswalk)

    spatial_xwalk = build_spatial_xwalk(
        flu_shp, old_flu_shp,
        spatial_new_key=SPATIAL_NEW_KEY,
        spatial_old_key=SPATIAL_OLD_KEY,
        spatial_overlap_cutoff=SPATIAL_OVERLAP_CUTOFF,
    )
    crosswalk = merge_spatial_and_fill(
        crosswalk, spatial_xwalk, old_zones,
        zone_new=ZONE_NEW,
        zone_old=ZONE_OLD,
        desc_old=DESC_OLD,
        spatial_new_key=SPATIAL_NEW_KEY,
        spatial_old_key=SPATIAL_OLD_KEY,
        spatial_overlap_cutoff=SPATIAL_OVERLAP_CUTOFF,
    )

    crosswalk = apply_manual_overrides(
        crosswalk, old_zones,
        working_xlsx=WORKING_XLSX,
        zone_new=ZONE_NEW,
        zone_old=ZONE_OLD,
        desc_old=DESC_OLD,
    )

    write_working_files(
        crosswalk, old_zones,
        working_xlsx=WORKING_XLSX,
        old_zones_xlsx=OLD_ZONES_XLSX,
    )

    if crosswalk["needs_review"].sum() > 0:
        raise RuntimeError(
            f"{crosswalk['needs_review'].sum()} rows still need review "
            f"-- check the working Excel file: {WORKING_XLSX}."
            f"\nAdd a 'manual match' or set 'confirmed_new' to True for each row, then re-run this step."
        )

    write_final_master(
        crosswalk,
        final_master_xlsx=FINAL_MASTER_XLSX,
        final_master_sheet=FINAL_MASTER_SHEET,
        zone_new=ZONE_NEW,
        zone_old=ZONE_OLD,
        desc_new=DESC_NEW,
        desc_old=DESC_OLD,
        new_yr=NEW_YR,
        old_yr=OLD_YR,
    )

    return context