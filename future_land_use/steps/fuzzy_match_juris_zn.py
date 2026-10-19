import geopandas as gpd
import pandas as pd
import re
from difflib import SequenceMatcher
from pathlib import Path

from future_land_use.util import Pipeline


def juris_zn_shp_to_table_comparison(shp_juris, table_juris):
    comparison = (
        pd.DataFrame({'juris_zn': sorted(set(shp_juris) | set(table_juris))})
        .assign(
            in_shp=lambda df: df['juris_zn'].isin(shp_juris),
            in_table=lambda df: df['juris_zn'].isin(table_juris)
        )
    )

    comparison['status'] = comparison.apply(
        lambda row: 'both' if row['in_shp'] and row['in_table']
        else 'only_shp' if row['in_shp']
        else 'only_table',
        axis=1
    )

    today = pd.Timestamp.today().strftime('%Y-%m-%d')
    return comparison


# fuzzy matching for zones in gis layer but not in table
def _normalize(s) -> str:
    s = str(s).strip().lower()
    return re.sub(r"[-_\s,/]+", "", s)


def fuzzy_match_juris(pipeline, shp_values, table_values, manual_path: Path):
    """For each shp value with no exact table match, find best fuzzy candidate.

    Reads manual overrides from `manual_path` if it exists (columns:
    juris_zn_shp, manual_match, confirmed_missing) and uses them.
    Returns a DataFrame with match results.
    """
    cfg = pipeline.settings['juris_zn_fuzzy_match_settings']
    shp_set = set(shp_values)
    table_set = set(table_values)
    missing = sorted(shp_set - table_set)

    # Load prior manual edits if present and schema matches
    prior_lookup = {}
    if manual_path.exists():
        prior = pd.read_csv(manual_path)
        if "juris_zn_shp" in prior.columns:
            ts = pd.Timestamp.now().strftime("%Y%m%d%H%M%S")
            prior.to_csv(manual_path.with_name(f"{manual_path.stem}_backup_{ts}.csv"), index=False)
            prior_lookup = prior.set_index("juris_zn_shp").to_dict("index")
        else:
            print(f"Note: {manual_path.name} exists but has unexpected schema; ignoring.")

    table_list = list(table_set)
    table_norm = {t: _normalize(t) for t in table_list}

    rows = []
    for shp_val in missing:
        norm_shp = _normalize(shp_val)

        # Best fuzzy candidate in table_juris
        best_score = 0.0
        best_cand = None
        for t in table_list:
            score = SequenceMatcher(None, norm_shp, table_norm[t]).ratio()
            if score > best_score:
                best_score = score
                best_cand = t

        suggested = best_cand if best_score >= cfg['min_cutoff'] else None
        auto = best_score >= cfg['auto_cutoff']

        prior_row = prior_lookup.get(shp_val, {})
        manual_match = prior_row.get("manual_match", "")
        confirmed_missing = prior_row.get("confirmed_missing", False)
        if pd.isna(manual_match):
            manual_match = ""
        if pd.isna(confirmed_missing):
            confirmed_missing = False

        manual_str = str(manual_match).strip()
        if manual_str:
            final_match = manual_str
            match_type = "manual"
            confidence = 1.0
        elif auto:
            final_match = suggested
            match_type = "fuzzy_auto"
            confidence = round(best_score, 3)
        elif suggested is not None:
            final_match = None
            match_type = "fuzzy_review"
            confidence = round(best_score, 3)
        else:
            final_match = None
            match_type = "no_match"
            confidence = round(best_score, 3)

        if isinstance(confirmed_missing, str):
            confirmed_flag = confirmed_missing.strip().upper() in ("TRUE", "1", "YES")
        else:
            confirmed_flag = bool(confirmed_missing)

        rows.append({
            "juris_zn_shp": shp_val,
            "suggested_match": suggested,
            "confidence": confidence,
            "match_type": match_type,
            "final_match": final_match,
            "manual_match": manual_str,
            "confirmed_missing": confirmed_flag,
        })

    return pd.DataFrame(rows)

def write_fuzzy_matches(flu_shp, juris_matches):
    """Replace unmatched juris_zn values in `flu_shp` with their manual/fuzzy match.

    Raises ValueError if any unmatched value has no match and is not confirmed_missing.
    """
    def _clean(series):
        return series.where(series.notna(), "").astype(str).str.strip()

    matches = juris_matches.copy()
    # manual_match takes precedence; final_match may be stale if the CSV was edited after the last run
    matches["resolved"] = _clean(matches["manual_match"])
    no_manual = matches["resolved"] == ""
    matches.loc[no_manual, "resolved"] = _clean(matches.loc[no_manual, "final_match"])

    unresolved = matches[(matches["resolved"] == "") & ~matches["confirmed_missing"].astype(bool)]
    if not unresolved.empty:
        raise ValueError(
            f"{len(unresolved)} juris_zn value(s) in the FLU shapefile have no match in the FLU table. "
            f"Edit juris_zn_shp_to_table_fuzzy.csv: enter a 'manual_match' or set 'confirmed_missing' to TRUE "
            f"for each of the following, then re-run:\n  "
            + "\n  ".join(unresolved["juris_zn_shp"].astype(str))
        )

    replacements = (
        matches[matches["resolved"] != ""]
        .set_index("juris_zn_shp")["resolved"]
    )
    flu_shp = flu_shp.copy()
    flu_shp["juris_zn"] = flu_shp["juris_zn"].replace(replacements)
    return flu_shp

def run_step(context):
    print("Running step: fuzzy_match_juriz_zn...")
    p = Pipeline(settings_path=context['configs_dir'])
    output_dir = p.get_output_path()
    data_dir = p.get_data_path()
    today = pd.Timestamp.today().strftime("%Y-%m-%d")

    # get shp 
    flu_shp = p.get_geodataframe('flu_shp')
    # get table
    flu_table = p.get_table('flu_table')
    # get juris_zn values from shp and table
    shp_juris = pd.Series(flu_shp['juris_zn'].dropna().unique(), name='juris_zn')
    table_juris = pd.Series(flu_table['juris_zn'].dropna().unique(), name='juris_zn')

    # generate the comparison of juris_zn between FLU shp and FLU table
    comparison = juris_zn_shp_to_table_comparison(shp_juris, table_juris)
    comparison.to_csv(output_dir / f"juris_zn_shp_to_table_comparison_{today}.csv")

    # fuzzy match juris_zn values between shp and table
    juris_match_csv =   data_dir / "juris_zn_shp_to_table_fuzzy.csv"
    juris_matches = fuzzy_match_juris(p, shp_juris, table_juris, juris_match_csv)
    juris_matches.to_csv(juris_match_csv, index=False)
    print(f"Wrote {len(juris_matches)} rows to {juris_match_csv}")
    print(juris_matches["match_type"].value_counts())

    flu_shp = write_fuzzy_matches(flu_shp, juris_matches)
    p.save_geodataframe(flu_shp,'flu_shp')

    return context
