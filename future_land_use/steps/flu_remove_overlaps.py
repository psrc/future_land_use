import getpass
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import MultiPolygon, Polygon
from shapely.errors import GEOSException
from shapely.ops import polygonize, unary_union


def remove_duplicate_records(gdf, attribute_cols=None):
    """Drop records that match on geometry plus the selected attribute columns."""
    if gdf.empty:
        return gdf.copy()

    out = gdf.copy()
    geometry_key = out.geometry.apply(
        lambda geom: geom.wkb_hex if geom is not None else None
    )
    if attribute_cols is None:
        attribute_cols = [col for col in out.columns if col != out.geometry.name]
    else:
        attribute_cols = [
            col
            for col in attribute_cols
            if col in out.columns and col != out.geometry.name
        ]

    duplicate_key = (
        out[attribute_cols].copy() if attribute_cols else pd.DataFrame(index=out.index)
    )
    duplicate_key["_geometry_key"] = geometry_key

    return out.loc[~duplicate_key.duplicated(keep="first")].copy()


def to_multipolygon(geom):
    """Extract polygon content from any geometry and return as MultiPolygon, or None."""
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    if hasattr(geom, "geoms"):
        polys = []
        for g in geom.geoms:
            if isinstance(g, Polygon):
                polys.append(g)
            elif isinstance(g, MultiPolygon):
                polys.extend(g.geoms)
        if polys:
            return MultiPolygon(polys)
    return None


def make_shapefile_safe(gdf):
    """Return a copy of gdf with schema adjusted for ESRI Shapefile limits."""
    out = gdf.copy()

    # These source-maintained fields often exceed DBF precision/width constraints.
    drop_cols = [
        c
        for c in out.columns
        if c.lower() in {"shape_area", "shape_leng", "shape_length"}
    ]
    if drop_cols:
        out = out.drop(columns=drop_cols)

    # Reduce float precision to lower DBF width pressure.
    float_cols = out.select_dtypes(include=["float", "float32", "float64"]).columns
    for col in float_cols:
        out[col] = out[col].round(3)

    # Enforce 10-character field names with uniqueness for Shapefile.
    new_names = {}
    used = set()
    for col in out.columns:
        if col == "geometry":
            continue
        base = str(col)[:10]
        candidate = base
        i = 1
        while candidate.lower() in used:
            suffix = str(i)
            candidate = f"{base[: 10 - len(suffix)]}{suffix}"
            i += 1
        used.add(candidate.lower())
        new_names[col] = candidate

    out = out.rename(columns=new_names)
    return out


def extract_overlap_areas(gdf):
    """Return one record per candidate feature for each atomic overlap area (2+ features)."""
    working = gdf.reset_index(drop=True).copy()

    working = working.loc[
        (~working.geometry.is_empty) & working.geometry.notna() & working.geometry.is_valid
    ].copy()
    working["feature_id"] = working.index
    attrs = working.drop(columns=["geometry"]).copy()

    # Build atomic polygon pieces from all boundaries so 3+ overlaps are a single shared piece.
    boundary_network = unary_union(working.geometry.boundary)
    piece_geoms = [
        geom
        for geom in polygonize(boundary_network)
        if (not geom.is_empty) and (geom.area > 0)
    ]

    if not piece_geoms:
        empty_cols = [
            "overlap_id", 
            "candidate_feature_id",
            "overlap_area",
            "overlap_count",
        ] + list(attrs.columns)
        empty = gpd.GeoDataFrame(columns=empty_cols, geometry=[], crs=working.crs)
        return empty

    pieces = gpd.GeoDataFrame(
        {"overlap_id": range(len(piece_geoms))},
        geometry=piece_geoms,
        crs=working.crs,
    )

    # Join piece representative points to source polygons to get piece membership.
    piece_points = gpd.GeoDataFrame(
        pieces[["overlap_id"]].copy(),
        geometry=pieces.representative_point(),
        crs=working.crs,
    )

    membership = gpd.sjoin(piece_points, working, how="inner", predicate="within")
    membership = membership[["overlap_id", "feature_id"]].drop_duplicates()

    overlap_counts = membership.groupby("overlap_id")["feature_id"].nunique()
    overlap_ids = overlap_counts[overlap_counts >= 2].index

    if len(overlap_ids) == 0:
        empty_cols = [
            "overlap_id",
            "candidate_feature_id",
            "overlap_area",
            "overlap_count",
        ] + list(attrs.columns)
        empty = gpd.GeoDataFrame(columns=empty_cols, geometry=[], crs=working.crs)
        return empty

    overlap_pieces = pieces[pieces["overlap_id"].isin(overlap_ids)].copy()
    overlap_pieces["overlap_area"] = overlap_pieces.geometry.area
    overlap_pieces["overlap_count"] = overlap_pieces["overlap_id"].map(overlap_counts)

    overlap_membership = membership[membership["overlap_id"].isin(overlap_ids)].copy()
    overlap_membership = overlap_membership.rename(
        columns={"feature_id": "candidate_feature_id"}
    )

    overlap_candidates = overlap_membership.merge(
        attrs, left_on="candidate_feature_id", right_on="feature_id", how="left"
    )
    overlap_candidates = overlap_candidates.drop(columns=["feature_id"])
    overlap_candidates = overlap_candidates.merge(
        overlap_pieces[["overlap_id", "overlap_area", "overlap_count", "geometry"]],
        on="overlap_id",
        how="left",
    )

    overlap_candidates = gpd.GeoDataFrame(
        overlap_candidates, geometry="geometry", crs=working.crs
    )

    return overlap_candidates


def apply_chosen_overlaps(base_gdf, chosen_overlaps):
    """Apply chosen overlap polygons by cutting them from base geometries, then appending.

    Subtracts the union of chosen overlap geometries from all base features,
    then appends the chosen overlap polygons back so each overlap area is represented once.
    """
    if chosen_overlaps.empty:
        return base_gdf.copy()

    def _repair_geometries(geo_series):
        try:
            repaired = geo_series.make_valid()
        except AttributeError:
            repaired = geo_series.buffer(0)
        repaired = repaired.where(~repaired.is_empty, None)
        return repaired

    chosen = chosen_overlaps.copy()
    chosen["geometry"] = _repair_geometries(chosen.geometry)
    chosen = chosen.loc[chosen.geometry.notna()].copy()
    overlap_union = unary_union(chosen.geometry)
    try:
        overlap_union = overlap_union.make_valid()
    except AttributeError:
        overlap_union = overlap_union.buffer(0)

    resolved = base_gdf.copy()
    resolved["geometry"] = _repair_geometries(resolved.geometry)
    resolved = resolved.loc[resolved.geometry.notna()].copy()

    try:
        resolved["geometry"] = resolved.geometry.difference(overlap_union)
    except GEOSException:
        def _safe_difference(geom):
            if geom is None:
                return None
            try:
                return geom.difference(overlap_union)
            except GEOSException:
                try:
                    fixed = geom.make_valid()
                except AttributeError:
                    fixed = geom.buffer(0)
                try:
                    return fixed.difference(overlap_union)
                except GEOSException:
                    return fixed.buffer(0).difference(overlap_union.buffer(0))

        resolved["geometry"] = resolved.geometry.apply(_safe_difference)

    resolved = resolved.loc[~resolved.geometry.is_empty].copy()

    for col in resolved.columns:
        if col not in chosen.columns:
            chosen[col] = None
    chosen = chosen[resolved.columns]

    combined = gpd.GeoDataFrame(
        pd.concat([resolved, chosen], ignore_index=True),
        geometry="geometry",
        crs=base_gdf.crs,
    )

    combined["geometry"] = combined.geometry.apply(to_multipolygon)
    combined = combined[combined.geometry.notna()].copy()

    return combined


def export_data(gdf, output_gdb, layer_name):
    """Export gdf to a layer in the output geodatabase."""
    if gdf.empty:
        print(f"Warning: {layer_name} is empty, skipping export.")
        return

    # Reuse shapefile-safe schema cleanup for consistent, portable field names/types.
    gdf = make_shapefile_safe(gdf)

    try:
        gdf.to_file(output_gdb, layer=layer_name, driver="OpenFileGDB")
        print(f"Exported {len(gdf)} records to {layer_name} in {output_gdb}")
    except Exception as e:
        print(f"Error exporting {layer_name}: {e}")


if Path().joinpath("C:/Users/", getpass.getuser(), "PSRC").exists():
    user_onedrive = Path().joinpath("C:/Users/", getpass.getuser(), "PSRC")
elif (
    Path()
    .joinpath("C:/Users/", getpass.getuser(), "Puget Sound Regional Council")
    .exists()
):
    user_onedrive = Path().joinpath(
        "C:/Users/", getpass.getuser(), "Puget Sound Regional Council"
    )
else:
    print("OneDrive path not found")

# Define source and output workspaces.
input_gdb = user_onedrive / "GIS - Sharing" / "Projects" / "FLU" / "FLU_draft2.gdb"
output_gdb = user_onedrive / "GIS - Sharing" / "Projects" / "FLU" / "scratch.gdb"

# Step 1: Read the source FLU polygons and repair any invalid geometries upfront.
flu_gdf = gpd.read_file(input_gdb, layer="FLU2025")
flu_gdf["geometry"] = flu_gdf.geometry.make_valid()
flu_gdf = flu_gdf.loc[(~flu_gdf.geometry.is_empty) & flu_gdf.geometry.notna()].copy()
print(f"The FLU has {len(flu_gdf)} records")

# Step 2: Build atomic overlap candidates — one row per (overlap zone x source feature).
overlap_candidates = remove_duplicate_records(extract_overlap_areas(flu_gdf))
print(f"Found {overlap_candidates['overlap_id'].nunique()} unique overlap areas")
print(f"Found {len(overlap_candidates)} candidate polygons for evaluation")

# Step 3: Derive the atomic overlap pieces layer — one polygon per overlap zone with no
# candidate attributes. Exporting this layer makes every overlap area visible as a distinct
# polygon in ArcGIS/QGIS for QA before committing to any winner selection.
overlap_pieces = (
    overlap_candidates
    .drop_duplicates(subset="overlap_id")
    [["overlap_id", "overlap_area", "overlap_count", "geometry"]]
    .reset_index(drop=True)
    .copy()
)
overlap_pieces = gpd.GeoDataFrame(overlap_pieces, geometry="geometry", crs=flu_gdf.crs)
print(f"Derived {len(overlap_pieces)} atomic overlap piece polygons")

# Step 4: Apply business-rule filters to find eligible winners.
winner_candidates = overlap_candidates[
    ~overlap_candidates["Juris"].isin(
        ["King_County", "Kitsap", "Pierce_County", "Snohomish_County"]
    )
]
winner_candidates = winner_candidates[winner_candidates["zone_psrc"] != "OS"]

# Step 5: Join one winner's attributes onto the clean atomic piece geometries.
# drop_duplicates guarantees exactly one winner row per overlap_id before the merge,
# so chosen_overlaps has one polygon per overlap zone with no geometry duplicates.
winner_attrs = (
    winner_candidates
    .drop_duplicates(subset="overlap_id", keep="first")
    .drop(columns=["geometry", "overlap_area", "overlap_count"])
)
chosen_overlaps = overlap_pieces.merge(winner_attrs, on="overlap_id", how="inner")
chosen_overlaps = gpd.GeoDataFrame(chosen_overlaps, geometry="geometry", crs=flu_gdf.crs)
print(f"Selected {len(chosen_overlaps)} overlap polygons to keep (one per overlap zone)")

# Step 6: Resolve overlaps by cutting chosen overlap areas from the base and adding winners back.

resolved_flu = apply_chosen_overlaps(flu_gdf, chosen_overlaps)
print(f"Resolved dataset has {len(resolved_flu)} records")


resolved_flu_dissolved = resolved_flu.dissolve(by=["Juris", "zone_psrc", "Juris_zn"], as_index=False)
resolved_flu_dissolved["geometry"] = resolved_flu_dissolved.geometry.apply(to_multipolygon)

# Step 7: Export intermediate and final layers for QA and downstream use.
export_data(flu_gdf, output_gdb, "FLU_Original")
export_data(overlap_candidates, output_gdb, "FLU_Overlap_Candidates")
export_data(overlap_pieces, output_gdb, "FLU_Overlap_Pieces")
export_data(chosen_overlaps, output_gdb, "FLU_Chosen_Overlaps")
export_data(resolved_flu, output_gdb, "FLU_Resolved")
export_data(resolved_flu_dissolved, output_gdb, "FLU_Resolved_Dissolved")

print("done")
