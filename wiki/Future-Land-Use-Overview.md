= Future Land Use =

The '''Future Land Use (FLU)''' repo builds the future land use / development constraints inputs used by the UrbanSim base year process. It replaces the older collection of standalone scripts in <code>urbansim-baseyear-prep</code> with a single, config-driven [https://pypyr.io/ pypyr] pipeline that:

* collects jurisdiction FLU tables/shapefiles, ElmerGeo layers, and manually digitized overlay layers,
* builds a crosswalk between the current and previous FLU vintages,
* imputes missing height/density values,
* applies HB 1110 (middle housing) density and transit overrides,
* spatially joins parcels to FLU zones (including overlays), and
* "unrolls" the result into a development constraints table for UrbanSim.

== Repo layout ==

* <code>future_land_use/run.py</code> — CLI entry point that launches the pypyr pipeline.
* <code>future_land_use/steps/</code> — one module per pipeline step (see [[#Pipeline steps|Pipeline steps]] below). Each module exposes a <code>run_step(context)</code> function, which is pypyr's convention for a runnable step.
* <code>future_land_use/r_scripts/</code> — R scripts (<code>imputation_FLU2026.R</code>, <code>load_FLU2026.R</code>) invoked from the <code>impute_flu</code> step for density/height imputation.
* <code>future_land_use/util/pipeline.py</code> — the <code>Pipeline</code> helper class (see [[#The Pipeline class|below]]).
* <code>projects/&lt;project_name&gt;/</code> — one folder per pipeline run/project (e.g. <code>projects/summer_2026</code>), containing:
** <code>configs/settings.yaml</code> — all paths and settings for that run.
** <code>data/</code> — small working files that are checked in/edited by hand, e.g. the overlay manual-match CSV.
** <code>output/</code> — everything the pipeline generates, including the <code>pipeline/</code> cache of intermediate Parquet tables and QC exports.

== Running the pipeline ==

1. Install [https://docs.astral.sh/uv/ uv]: <code>powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"</code>
2. Create a new project by copying an existing <code>projects/&lt;name&gt;</code> folder (or edit one in place) and update the paths/settings in its <code>configs/settings.yaml</code>.
3. Run the pipeline, pointing at the project's config directory:

 <code>uv run future_land_use\run.py -c projects\summer_2026\configs</code>

The <code>steps:</code> list at the bottom of <code>settings.yaml</code> controls which steps run and in what order. Steps can be commented out to re-run only part of the pipeline (later steps read intermediate tables saved by earlier ones, so upstream steps must have been run at least once).

== The Pipeline class ==

<code>future_land_use/util/pipeline.py</code> defines a <code>Pipeline</code> object that every step creates from the project's <code>configs_dir</code>. It:

* loads <code>settings.yaml</code> and resolves <code>data_dir</code>/<code>output_dir</code> relative to the project root,
* creates the <code>data/</code> and <code>output/</code> directories if they don't exist,
* saves/loads intermediate tables as Parquet files under <code>output/pipeline/</code> (<code>save_table</code> / <code>get_table</code>), and
* saves/loads GeoDataFrames the same way, storing geometry as WKT so it survives the round-trip through Parquet (<code>save_geodataframe</code> / <code>get_geodataframe</code>).

This is what lets each step be re-run independently — a step reads whatever tables it needs from <code>output/pipeline/</code> and writes its results back for the next step to pick up.

== Configuration (settings.yaml) ==

Each project's <code>configs/settings.yaml</code> is organized into a global section plus one settings block per pipeline stage:

* '''Global''' — <code>data_dir</code>, <code>output_dir</code>, <code>root_dir</code> (shared network location for large inputs/outputs), <code>elmer_geo_layers</code>, the FLU table (<code>flu_table_path</code>/<code>flu_table_sheet</code>) and FLU shapefile (<code>flu_shp</code>) paths and column-rename maps.
* '''<code>old_flu_crosswalk_settings</code>''' — inputs and match-confidence cutoffs for the [[#Old FLU Crosswalk Builder|old FLU crosswalk step]].
* '''<code>imputation_settings</code>''' — paths for the [[#FLU Imputation|R imputation step]].
* '''<code>overlay_settings</code>''' — the overlay GDB path and per-jurisdiction layer list for the [[#Combine Overlays|combine overlays step]].
* '''<code>hb1110_settings</code>''' — the transit walkshed layer and output options for [[#Flag HB 1110 Parcels|HB 1110 flagging]].
* '''<code>unroll_constraints_settings</code>''' — parcel/base-year cache paths and the <code>apply_hb_1110</code> switch used by the later parcel/constraint steps.
* '''<code>steps</code>''' — the ordered list of pipeline steps to execute.

== Pipeline steps ==

Steps are listed in the order they run (per <code>settings.yaml</code>).

=== Load Data ===
<code>future_land_use/steps/load_data.py</code>

Reads all of the flat-file inputs needed by later steps and caches them to the pipeline:
# the new FLU shapefile (<code>flu_shp</code>),
# parcel land use type / TOD / gross sqft from the UrbanSim base year cache,
# the new FLU Excel table (<code>flu_table_path</code>),
# the old-vintage FLU shapefile, and
# the old FLU crosswalk lookup table.

=== Get Elmer Data ===
<code>future_land_use/steps/get_elmer_data.py</code>

Downloads each layer listed in <code>elmer_geo_layers</code> (e.g. parcel points, cities) from ElmerGeo via <code>psrcelmerpy</code> and saves them to the pipeline for use by later spatial-join steps.

=== Combine Overlays ===
<code>future_land_use/steps/combine_overlays.py</code>

Combines the manually digitized jurisdiction overlay layers (e.g. University Place, Poulsbo, Silverdale, Tacoma) listed in <code>overlay_settings.overlay_layers</code> into a single layer and matches each overlay zone to a row in the FLU table.

# '''Load overlay layers''' — read each layer from the overlay GDB, build a <code>juris_zn</code> id (from either <code>juris_zn_col</code> or <code>zone_col</code> + jurisdiction name), and dissolve by <code>juris_zn</code>.
# '''Apply manual matches''' — re-apply any prior corrections from <code>data/overlay_manual_match.csv</code> (a timestamped backup of the previous file is kept), then merge against the FLU table.
# '''Flag unmatched zones''' — any overlay zone that still doesn't match a <code>juris_zn</code> in the FLU table is written back out to <code>overlay_manual_match.csv</code> for manual review; if any zones remain unmatched after that file has been filled in and the step re-run, the step raises an error rather than continuing.
# '''Save outputs''' — the combined overlay layer is cached to the pipeline and also written to <code>output/overlay_layers_merged.gdb</code> for visual QC. If <code>write_final_overlays_to_input_gdb</code> is <code>True</code>, a dated copy is also written back into the source overlay GDB.

'''Configure''': <code>overlay_gdb_path</code>, <code>overlay_layers</code> (one entry per jurisdiction/layer), <code>write_final_overlays_to_input_gdb</code>, <code>final_overlay_layer_name</code>.

=== Flag Overlay Parcels ===
<code>future_land_use/steps/flag_overlay_parcels.py</code>

Spatially joins parcel points to the combined overlay layer and saves a simple <code>parcel_id</code> → <code>juris_zn</code> lookup (<code>overlay_parcels</code>) used later by [[#Apply Overlays|Apply Overlays]].

=== Flag HB 1110 Parcels ===
<code>future_land_use/steps/flag_hb_1110_parcels.py</code>

Identifies which parcels fall in an HB 1110 (middle housing) tiered city and, for tier 1/2 cities, whether they also fall within a transit walkshed.

# Filter the <code>cities</code> layer to those with <code>hb_1110_tier &gt; 0</code> and spatially join parcels to them.
# Spatially join those parcels against the HB 1110 transit walkshed layer and set <code>hb_1110_transit = 1</code> where they overlap (tier 3 cities never get the transit flag, since only tiers 1–2 have a transit requirement).
# Save the parcel-level flags (<code>hb_1110_parcels</code>) for use by later HB 1110 steps.
# Optionally (<code>output_cities_walkshed: True</code>), dissolve full parcel polygons by city/tier/transit flag and export to <code>output/hb_1110_cities_walkshed.gdb</code> for a visual QC check — this can be slow, so it's off by default.

'''Configure''': <code>transit_gdb_path</code>, <code>transit_walksheds_layer</code>, <code>output_cities_walkshed</code>, <code>output_cities_walkshed_name</code>.

=== Old FLU Crosswalk Builder ===
<code>future_land_use/steps/create_old_flu_crosswalk.py</code>

Builds a crosswalk between the current FLU vintage and the previous vintage, which the imputation step then uses to carry forward previously-collected values. This step replaces the standalone <code>old_flu_crosswalk.py</code> script from <code>urbansim-baseyear-prep</code>.

==== Workflow ====

Given the new FLU table/shapefile, the old FLU shapefile, and an old description lookup, the script produces a crosswalk by running:

# '''Load sources''' — new FLU table, old FLU shapefile (+ its description lookup), and the new FLU shapefile geometry used for the spatial step.
# '''Unique zone lists''' — one row per <code>(jurisdiction, zone)</code> in each vintage.
# '''Name-based match''' (per jurisdiction, three passes):
#* '''exact''' — identical zone strings (<code>confidence = 1.0</code>)
#* '''near_match''' — equal after lowercasing and stripping separators (<code>confidence = 0.8</code>)
#* '''fuzzy''' — best <code>SequenceMatcher</code> ratio above <code>fuzzy_match_cutoff</code> (<code>confidence = ratio</code>)
#* Any new zone with no old match gets <code>match_type = 'new_zone'</code>.
# '''Spatial fill''' — for any row still flagged <code>needs_review</code>, look at the polygon overlap between the new zone and every old zone and take the old zone with the greatest share of new-zone area. If the overlap is at or above <code>spatial_overlap_cutoff</code> the row is auto-accepted (<code>match_type = 'spatial'</code>, <code>needs_review = False</code>); otherwise it stays flagged for review but the spatial suggestion is recorded.
# '''Manual overrides''' — re-reads the working Excel (from a previous run) and applies any user edits in the <code>manual_match</code> / <code>confirmed_new</code> columns.
# '''Write outputs''' — a working Excel (for iterative review) and a cleaned-up <code>Full_FLU_Master_Corres_File.xlsx</code>-style crosswalk for downstream use by the imputation step.

'''Configure''': <code>old_flu_crosswalk_settings</code> — <code>old_flu_shp</code>, <code>old_crosswalk</code> (+ sheet/rename settings), <code>fuzzy_match_cutoff</code>, <code>spatial_overlap_cutoff</code>, <code>crosswalk_output_dir</code>.

=== FLU Imputation ===
<code>future_land_use/steps/impute_flu.py</code> (calls <code>future_land_use/r_scripts/imputation_FLU2026.R</code>)

Imputes max height and densities for zones where they weren't collected. The hierarchy for values is:
# use collected values
# where values were not collected, update with previously used values (via the old FLU crosswalk)
# for any remaining gaps, values are imputed based on max height, land coverage (LC), and urban/rural designation

The Python step builds the command-line arguments (input/output dirs, master lookup, new/old FLU paths) from <code>settings.yaml</code>, runs the R script as a subprocess, then reads the resulting <code>final_flu_imputed_&lt;date-created&gt;.csv</code> (most recent by date suffix) back into the pipeline as <code>flu_imputed</code>.

'''Configure''': <code>imputation_settings</code> — <code>r_executable_path</code>, <code>r_script_path</code>, <code>output_dir</code>, <code>old_flu_crosswalk</code>, <code>old_flu</code>; plus <code>unroll_constraints_settings.juris_zn_imputed_id</code> (unique id column in the imputed output).

=== Apply HB 1110 to FLU ===
<code>future_land_use/steps/apply_hb_1110_to_flu.py</code>

Applies HB 1110 minimum-density rules to the imputed FLU table (zone-level, not parcel-level):

# Build a <code>city_name</code> → <code>hb_1110_tier</code> lookup from the flagged HB 1110 parcels and validate that every HB city name matches a <code>Juris</code> value in the imputed FLU data.
# Apply minimum <code>FloorMaxDU_lot</code> density floors for residential/mixed-use zones: 4 du/lot for tier 1 cities, 2 du/lot for tier 2–3 cities.
# For residential/mixed-use zones in tier 1–2 cities that also contain transit parcels, create additional <code>_hb_transit</code> zone variants with higher minimums (6 du/lot for tier 1, 4 du/lot for tier 2–3) and flag them with <code>hb_transit_override</code>.
# Save the combined result as <code>flu_imputed_hb_1110</code>.

=== Parcel-FLU Spatial Join ===
<code>future_land_use/steps/parcel_flu_spatial_join.py</code>

Assigns a <code>plan_type_id</code> to every FLU zone and spatially joins parcels to it.

# Assign a unique <code>plan_type_id</code> to each row of the imputed FLU table (with or without HB 1110 applied, per <code>apply_hb_1110</code>) and merge it onto the FLU shapefile geometry.
# Load parcel points and merge in land use type/TOD/gross sqft.
# Spatially join parcels to the FLU shapefile; any parcel with no match (expected to be a small fraction) is instead joined to its nearest FLU polygon. If more than 5% of parcels have no direct spatial match, the step raises an error so the mismatch can be investigated.
# Deduplicate parcels that matched more than one FLU polygon (overlapping zones), preferring city-level <code>juris_zn</code> values over county-level ones, and export QC files listing affected parcels (<code>flu_qc/pins_multi_*.csv</code>, a GDB layer of the overlapping points) and which jurisdiction/zone pairs most often overlap (<code>flu_qc/flu_juris_zn_pair_counts_*.csv</code>).
# Save the parcel → <code>plan_type_id</code> crosswalk as <code>parcel_plan_type_xwalk</code>.

=== Apply Overlays ===
<code>future_land_use/steps/apply_overlays.py</code>

Where a parcel falls on both a manually digitized overlay zone and an underlying FLU zone, this step creates a new combined "overlay combo" plan type rather than picking one or the other.

# Find parcels that land on both an overlay zone and an underlying zone, and group them by their unique combination of <code>plan_type_id</code>s (<code>overlay_combo_id</code>).
# For each combination, build a new zone row by taking the '''minimum''' of each density/height/coverage field across the combined plan types, and construct a new <code>juris_zn</code> (underlying zone name + overlay zone suffix) and a new <code>plan_type_id</code>.
# Append the new overlay-combo rows to the FLU table and remap affected parcels in the parcel crosswalk to their new <code>plan_type_id</code>.
# Save <code>flu_imputed_with_overlays</code> / <code>flu_imputed_hb_1110_with_overlays</code> and <code>parcel_plan_type_xwalk_with_overlays</code>.

=== Apply HB 1110 to Parcels ===
<code>future_land_use/steps/apply_hb_1110_to_parcels.py</code>

Only runs when <code>apply_hb_1110</code> is <code>True</code>. Re-applies the HB 1110 transit override at the parcel level (after overlays have been merged in): parcels in tier 1–2 transit walksheds that are residential/mixed-use get their <code>juris_zn</code> suffixed with <code>_hb_transit</code> and their <code>plan_type_id</code> remapped to the corresponding <code>hb_transit_override</code> plan type. Saves <code>parcel_plan_type_xwalk_with_overlays_hb_1110</code>.

=== Unroll Constraints ===
<code>future_land_use/steps/unroll_constraints.py</code>

The final step: converts the wide, one-row-per-zone FLU table into a long development constraints table (one row per <code>plan_type_id</code> + <code>generic_land_use_type_id</code> + <code>constraint_type</code>) for UrbanSim, and finalizes the parcel → plan type crosswalk.

# Convert lot coverage (LC) columns from percent to proportion.
# '''Unroll''' each use type into its own constraint rows: single-family and multi-family units-per-acre and units-per-lot, office/commercial/industrial/mixed FAR, and mixed-use units-per-acre.
# Combine all unrolled rows, clamp any row where <code>minimum &gt; maximum</code>, and run consistency QC (plan types present in the FLU table but missing from the constraints table, or with an all-zero maximum) — written to <code>output/&lt;root&gt;/unroll_constraints/ptid_qc/</code>.
# Add fixed zero-value "lockout" constraint rows (<code>plan_type_id = 9999</code>) plus per-land-use-type lockout plan types (e.g. schools, government, hospitals, forest, agriculture, vacant undevelopable) and remap any parcel with a null/unmatched plan type to the appropriate lockout id.
# Produce an HB 1110 capacity summary CSV grouped by jurisdiction/tier/transit-override.

'''Configure''' (<code>unroll_constraints_settings</code>): <code>flu_imputed_dir</code>, <code>juris_zn_imputed_id</code>, <code>base_year_parcel_layer</code>, <code>base_year_cache</code>, <code>parcel_id_col</code>, <code>apply_hb_1110</code>.
