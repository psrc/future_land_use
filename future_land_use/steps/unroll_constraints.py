import os
import pandas as pd
import geopandas as gpd
import numpy as np
from datetime import date
from itertools import combinations
from future_land_use.util import check_multi_pins
from future_land_use.util import Pipeline

def _apply_min_du_lot(df, tier_vals, res_cond, min_val, du_lot_col, flag_override=None):
    mask = df['hb_1110_tier'].isin(tier_vals) & res_cond
    below_min = mask & (df[du_lot_col] < min_val)
    was_na = mask & (df[du_lot_col].isna())
    df.loc[mask, du_lot_col] = df.loc[mask, du_lot_col].fillna(min_val).apply(lambda x: max(x, min_val))
    if isinstance(flag_override, str) and (below_min | was_na).any():
        df.loc[below_min | was_na, flag_override] = 1
    return df

def run_step(context):
    # config ----------------------------------------------------
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings.get('unroll_constraints_settings', {})

    # root dir
    ROOT = cfg['root_dir']

    # output dir
    OUTPUT = os.path.join(ROOT, "unroll_constraints")

    # flu gis layer
    # flu_shp_path = os.path.join(flu_input_dir, "FLU_draft2.gdb")
    # flu_layer = "FLU2025" # name of layer within gdb
    FLU_SHP = cfg['flu_shp']
    FLU_SHP_PATH = os.path.join(ROOT, FLU_SHP)
    juris_zn_shp_id = cfg['juris_zn_shp_id'] # unique id column

    # imputed FLU data directory (output from imputation.R)
    FLU_IMP = cfg['flu_imputed'] 
    FLU_IMP_PATH = os.path.join(ROOT, FLU_IMP)
    juris_zn_imputed_id = cfg['juris_zn_imputed_id'] # unique id column

    # parcels file
    BASE_YEAR_PRCL_LAYER = cfg['base_year_parcel_layer'] # ElmerGeo layer name for parcel points

    # urbansim baseyear cache
    CACHE = cfg['base_year_cache']  # BY 2023

    # read/process files ----------------------------------------------------

    # read in flu gis layer
    print(f"Reading in FLU shapefile {FLU_SHP_PATH}...")
    flu_shp = (
        # gpd.read_file(flu_shp_path, layer = flu_layer)
        gpd.read_file(FLU_SHP_PATH)
        .rename(columns={juris_zn_shp_id:'juris_zn'})
    )[['juris_zn', 'geometry']]
    flu_shp = flu_shp.to_crs(epsg=2285)

    # read in flu imputed data
    print(f"Reading in FLU imputed data {FLU_IMP_PATH}...")
    f = (
        pd.read_csv(FLU_IMP_PATH)
        .rename(columns={juris_zn_imputed_id:'juris_zn'})
    )

    # clean up f; remove extra/unecessary fields before join
    f_col_keep = [col for col in f.columns if col not in ['Key', 'Zone', 'Definition'] + list(f.columns[f.columns.str.endswith("src")])]
    f = f[f_col_keep]

    # bring in HB1110 parcels
    print("Reading in HB1110 parcels...")
    hb_parcels = p.get_table('hb_1110_parcels')
    # pull out unique city names and tier values from HB1110 parcels
    hb_cities = hb_parcels.groupby('city_name').agg({'hb_1110_tier':'first'}).reset_index()
    hb_cities['city_name'] = hb_cities['city_name'].str.replace(' ', '_', regex=True)

    # check that Juris and city-name values match between imputed FLU data and HB parcels data
    if not hb_cities.loc[~hb_cities['city_name'].isin(f['Juris'].unique().tolist())].empty:
        raise ValueError('Some Juris values in imputed FLU data do not match city names in HB parcels data. Check for typos or mismatches.\n'
                        f'Mismatched values: {hb_cities.loc[~hb_cities["city_name"].isin(f["Juris"].unique().tolist()), "city_name"].tolist()}')

    #-------------- apply HB1110 rules----------------------------------------------------------
    print("Applying HB 1110 rules...")
    # merge HB1110 tier values into imputed FLU data
    f = f.merge(hb_cities, left_on='Juris', right_on='city_name', how='left')

    # apply minimum density rules for HB 1110 tiers
    du_lot_col = 'FloorMaxDU_lot'
    print("Applying minimum density rules for HB 1110 tiers...")
    f = _apply_min_du_lot(f, [1], (f['Res_Use'] == 1) | (f['Mixed_Use'] == 1), 4, du_lot_col)
    f = _apply_min_du_lot(f, [2, 3], (f['Res_Use'] == 1) | (f['Mixed_Use'] == 1), 2, du_lot_col)

    # create list of parcel_ids that are in HB 1110 transit zones (tier 1 or 2 and transit=1)
    hb_transit_parcels = hb_parcels.loc[(hb_parcels['hb_1110_tier'].isin([1,2])) & (hb_parcels['hb_1110_transit'] == 1),'parcel_id'].tolist()
    # create list of city names that have parcels in HB 1110 transit zones
    hb_transit_cities = hb_parcels.loc[hb_parcels['parcel_id'].isin(hb_transit_parcels),'city_name'].unique().tolist()
    # filter imputed FLU data for Juris values in the hb transit cities list that are residential or mixed-use
    flu_hb_transit = f.loc[f.Juris.isin(hb_transit_cities) & ((f['Res_Use'] == 1) | (f['Mixed_Use'] == 1))]

    # apply minimum density rules to HB 1110 transit zones
    flu_hb_transit = flu_hb_transit[
        ((flu_hb_transit['hb_1110_tier'] == 1) & (flu_hb_transit[du_lot_col] < 6)) |
        ((flu_hb_transit['hb_1110_tier'].isin([2, 3])) & (flu_hb_transit[du_lot_col] < 4))
    ]
    flu_hb_transit['juris_zn'] = flu_hb_transit['juris_zn'] + '_hb_transit'
    print("Creating minimum density rules for HB 1110 transit zones...")
    flu_hb_transit = _apply_min_du_lot(flu_hb_transit, [1], (flu_hb_transit['Res_Use'] == 1) | (flu_hb_transit['Mixed_Use'] == 1), 6, du_lot_col, flag_override='hb_transit_override')
    flu_hb_transit = _apply_min_du_lot(flu_hb_transit, [2, 3], (flu_hb_transit['Res_Use'] == 1) | (flu_hb_transit['Mixed_Use'] == 1), 4,du_lot_col, flag_override='hb_transit_override')

    # add the new hb_transit zones to the main imputed FLU data
    f = pd.concat([f, flu_hb_transit], ignore_index=True)

    # create plan_type_id for each juris_zn in imputed FLU data
    f['plan_type_id'] = np.arange(len(f)) + 1 # assign plan_type_id

    # join imputed data back to FLU shapefile
    flu = flu_shp.merge(f, on = ['juris_zn'], how = 'left')

    # spatial join parcels to flu to assign plan_type_id------------------------------------------------

    # read parcels file & lu type file
    print("Reading in parcels...")
    prcls = p.get_geodataframe('parcels_pts')[['parcel_id', 'geometry']]
    pin_name = "parcel_id" # BY 2023

    print(f"Reading in parcel land use type from urbansim cache {CACHE}...")
    #prcls_pin = np.fromfile(os.path.join(CACHE, 'parcel_id.li4'), np.int32) # BY 2018
    #prcls_lu = np.fromfile(os.path.join(CACHE, 'land_use_type_id.li4'), np.int32) # BY 2018
    prcls_pin = np.fromfile(os.path.join(CACHE, 'parcel_id.li8'), np.int64) # BY 2023
    prcls_lu = np.fromfile(os.path.join(CACHE, 'land_use_type_id.li8'), np.int64) # BY 2023
    prcls_tod = np.fromfile(os.path.join(CACHE, 'tod_id.li4'), np.int32)
    prcls_sqft = np.fromfile(os.path.join(CACHE, 'gross_sqft.lf8'), np.float64)
    lu_type = pd.DataFrame({pin_name:prcls_pin, 'lu_type':prcls_lu, 'tod_id':prcls_tod, 'gross_sqft':prcls_sqft}, index = prcls_pin)
    #lu_type['PIN'] = lu_type['PIN'].astype(np.int64) # BY 2018

    prcls[pin_name] = prcls[pin_name].astype(np.int64)
    prcls = prcls.merge(lu_type, on = pin_name)

    # spatial join parcels to flu shp
    print("Spatially joining parcels to FLU shapefile...")
    prcls_flu = gpd.sjoin(prcls, flu,how = 'left')
    # separate out parcels that did not join to flu (i.e. those with no juris_zn match)
    unmatched = prcls_flu.loc[prcls_flu['juris_zn'].isna(),['parcel_id','geometry','lu_type','tod_id','gross_sqft']].copy()
    print(f"Number of parcels with no spatial match to FLU shp: {len(unmatched)} out of {len(prcls)} total parcels. Unmatched parcels will be joined to nearest FLU polygon.")
    if len(unmatched) > len(prcls) * 0.05: # if more than 5% of parcels have no match throw an error
        raise RuntimeError(f"Warning: {len(unmatched)} parcels have no spatial match to FLU shapefile, which is more than 5% of total parcels. Check data and spatial join parameters.")
    # for unmatched parcels, assign plan_type_id based on nearest flu polygon
    unmatched_flu = gpd.sjoin_nearest(unmatched, flu)
    # remove unmatched parcels from prcls_flu
    prcls_flu = prcls_flu.loc[~prcls_flu['juris_zn'].isna()].copy()
    # concat the sjoin nearest results back to prcls_flu
    prcls_flu = pd.concat([prcls_flu, unmatched_flu], ignore_index=True)

    # flag parcels with no match in imputed flu data (plan_type_id is still null)
    prcls_flu['no_flu_match'] = 0
    prcls_flu.loc[prcls_flu['plan_type_id'].isna(),'no_flu_match'] = 1

    # QC FLU shapefile------------------------------------------------------------------

    check_multi_pins(prcls_flu, OUTPUT, pin_name = pin_name) #check code

    # check for one-to-many records in flu overlay
    prcls_flu[pin_name].duplicated().any()
    duplicate = prcls_flu[prcls_flu.duplicated(pin_name)][[pin_name]] #221
    dup_df = prcls_flu[prcls_flu[pin_name].isin(duplicate[pin_name])].sort_values(by=[pin_name]) #417 (BY 2018)

    # count frequency of PINs
    dup_pin_freq = dup_df.groupby([pin_name])[pin_name].count().reset_index(name='counts')
    triple_pin = dup_pin_freq[dup_pin_freq['counts']>2] # handle triple count pins separately

    # de-duplicate parcels with multiple matches in flu spatial join, 
    # keeping non-county-level juris_zn matches where possible (i.e. prioritize city-level over county-level matches)
    county_juris_zns = [
        'king_county',
        'snohomish_county',
        'pierce_county',
        'kitsap_county',
        'king',
        'pierce',
        'kitsap'
        'snoco'
        # snohomish # leave out Snohomish because it is also a muni
    ]

    # re-assemble all parcels
    unjoined = prcls[~prcls[pin_name].isin(prcls_flu[pin_name])]
    x1 = prcls_flu[~prcls_flu[pin_name].isin(dup_df[pin_name])] # records with no duplicates

    x2 = dup_df[~dup_df['plan_type_id'].isnull() & ~dup_df[pin_name].isin(triple_pin[pin_name])] # duplicates where plan_type_id is not null. Excludes triple_pin
    x2a = dup_df[dup_df[pin_name].isin(triple_pin[pin_name]) & ~dup_df['plan_type_id'].isnull()] # triple_pin where plan_type_id is not null

    # sort so county-level juris_zn rows (lowest priority) come last within each pin group
    x2['_is_county'] = x2['juris_zn'].str.lower().str.startswith(tuple(county_juris_zns))
    x2 = x2.sort_values(by=[pin_name, '_is_county'])
    x2_kp_first = x2.drop_duplicates(subset=[pin_name], keep='first').drop(columns=['_is_county']) # remove duplicates (keep first non-county)

    x2a['_is_county'] = x2a['juris_zn'].str.lower().str.startswith(tuple(county_juris_zns))
    x2a = x2a.sort_values(by=[pin_name, '_is_county'])
    x2a_kp_first = x2a.drop_duplicates(subset=[pin_name], keep='first').drop(columns=['_is_county']) # remove duplicates amongst triple pins (keep first non-county)

    # append all tables
    len(prcls) - (len(x1) + len(unjoined) + len(x2_kp_first) + len(x2a_kp_first))
    all_df = pd.concat([x1, x2_kp_first, x2a_kp_first, unjoined])

    # For each duplicated parcel_id, collect the sorted set of juris_zn values it intersects,
    # then enumerate all juris_zn pairs and count how often each pair co-occurs on a parcel.
    juris_pairs = (
        dup_df.groupby(pin_name)['juris_zn']
            .apply(lambda s: list(combinations(sorted(s.dropna().unique()), 2)))
    )

    pair_counts = (
        pd.Series([p for pairs in juris_pairs for p in pairs])
        .value_counts()
        .rename_axis('juris_zn_pair')
        .reset_index(name='n_duplicated_parcels')
    )

    pair_counts[['juris_zn_1', 'juris_zn_2']] = pd.DataFrame(
        pair_counts['juris_zn_pair'].tolist(), index=pair_counts.index
    )
    pair_counts = pair_counts[['juris_zn_1', 'juris_zn_2', 'n_duplicated_parcels']]
    pair_counts.to_csv(os.path.join(OUTPUT,"flu_qc", 'flu_juris_zn_pair_counts_' + str(date.today()) + '.csv'), index=False)

    #----------------- apply overlays ----------------------------------------------
    print("Applying overlays...")
    overlay_parcels = (
        p.get_table('overlay_parcels')
        .merge(f,on='juris_zn')
        .rename(columns={'juris_zn':'overlay_juris_zn'})
        [['parcel_id','overlay_juris_zn','plan_type_id']]
    )

    parcels_in_overlay = (
        all_df.loc[
            all_df['parcel_id'].isin(overlay_parcels['parcel_id']),
            ['parcel_id','juris_zn','plan_type_id']]
            .copy().dropna()
    )
    parcels_in_overlay.rename(columns={'juris_zn':'orig_juris_zn'}, inplace=True)
    parcels_to_update = pd.concat([overlay_parcels, parcels_in_overlay])

    # drop non-duplicates because if there's only 1 parcel_id then it means it must land 
    # on an overlay but not another underlying zone
    overlay_only = parcels_to_update.duplicated(subset='parcel_id')
    print(f"Parcels that land on an overlay but not another underlying zone: {len(parcels_to_update[overlay_only])}")
    parcels_to_update = parcels_to_update[overlay_only].copy()

    # find all existing combinations of plan_type_id based on parcel_id
    plan_type_combos = (
        parcels_to_update
        .groupby('parcel_id')['plan_type_id']
        .apply(lambda x: sorted(set(x)))  # sorted unique plan_type_ids per parcel
        .reset_index(name='plan_type_id_combo')
    )
    print(f"Unique parcel_id + plan_type_id combos: {len(plan_type_combos)}")
    plan_type_combos

    # get unique plan_type_id combinations (convert list to tuple for hashing)
    plan_type_combos['plan_type_id_combo'] = plan_type_combos['plan_type_id_combo'].apply(tuple)
    unique_combos = plan_type_combos['plan_type_id_combo'].unique()
    print(f"Unique plan_type_id combos created by overlays: {len(unique_combos)}")

    # create overlay_combo_id mapping
    combo_to_id = {combo: idx + 1 for idx, combo in enumerate(unique_combos)}
    plan_type_combos['overlay_combo_id'] = plan_type_combos['plan_type_id_combo'].map(combo_to_id)

    # add overlay_combo_id back to parcels_to_update
    parcels_to_update = parcels_to_update.merge(
        plan_type_combos[['parcel_id', 'overlay_combo_id']],
        on='parcel_id',
        how='left'
    )

    # get unique overlay combinations with the underlying plan_type_id
    overlay_plan_types = parcels_to_update.drop_duplicates(subset=['plan_type_id', 'overlay_combo_id']).drop(columns=['parcel_id'])
    # merge overlay_plan_types with f on plan_type_id
    overlay_plan_types_out = overlay_plan_types.merge(f, on='plan_type_id', how='left')
    # Remove the jurisdiction name + underscore from overlay_juris_zn (e.g. "Tacoma_STPG" -> "STPG")
    overlay_plan_types_out['overlay_juris_zn'] = overlay_plan_types_out.apply(
        lambda row: row['overlay_juris_zn'].replace(str(row['Juris']) + '_', '', 1)
        if pd.notna(row['Juris']) and pd.notna(row['overlay_juris_zn'])
        else row['overlay_juris_zn'],
        axis=1
    )
    # replace -1 values in use columns with NaN for aggregation
    use_cols = ['Res_Use', 'Comm_Use', 'Office_Use', 'Indust_Use', 'Mixed_Use']
    overlay_plan_types_out[use_cols] = overlay_plan_types_out[use_cols].replace(-1,np.nan)

    # aggregate to get minimum values for each overlay_combo_id
    min_cols = use_cols + [
                'orig_juris_zn','MinDU_Res','MinFAR_Comm', 'MinFAR_Office', 'MinFAR_Indust', 'MinFAR_Mixed',
                'MaxDU_Res','MaxFAR_Comm', 'MaxFAR_Office', 'MaxFAR_Indust', 'MaxFAR_Mixed', 'MaxHt_Res',
                'MaxHt_Comm', 'MaxHt_Office', 'MaxHt_Indust', 'MaxHt_Mixed', 'LC_Res', 'LC_Comm',
                'LC_Office', 'LC_Indust','LC_Mixed','SingleFamily_Use','MultiFamily_Use'
    ]
    overlay_plan_types_out.to_csv(os.path.join(OUTPUT, "flu_qc", 'overlay_plan_types_out_pre_agg_' + str(date.today()) + '.csv'), index=False)
    agg_col_dict = {col: 'min' for col in min_cols}
    agg_col_dict.update({'overlay_juris_zn': lambda x: '_'.join(x.dropna().astype(str))})
    overlay_plan_types_out = overlay_plan_types_out.groupby('overlay_combo_id').agg(agg_col_dict).reset_index()

    # assign new plan_type_id for each overlay_combo_id
    overlay_plan_types_out['plan_type_id'] = len(f) + np.arange(len(overlay_plan_types_out)) + 1

    # create new juris_zn for overlay combinations
    overlay_plan_types_out['juris_zn'] = overlay_plan_types_out['orig_juris_zn'] + overlay_plan_types_out['overlay_juris_zn']
    overlay_plan_types_out.drop(columns=['orig_juris_zn','overlay_juris_zn'], inplace=True)

    # add overlay combo zones back to original imputed FLU data
    f = pd.concat([f, overlay_plan_types_out], ignore_index=True)
    # turn -1 values in the use columns back into 0s
    f[use_cols] = f[use_cols].replace(-1,np.nan).fillna(0)

    #-------- apply overlays and HB1110 rules to parcels ---------------------------------

    # update plan_type_id in all_df for parcels that have an overlay_combo_id
    parcels_to_update = parcels_to_update[['parcel_id','overlay_combo_id']].merge(f[['plan_type_id','overlay_combo_id']], on='overlay_combo_id', how='left')[['parcel_id','plan_type_id']]

    # replace plan_type_id in all_df with plan_type_id from parcels_to_update, only for matching parcels
    parcel_to_new_ptid = parcels_to_update.drop_duplicates(subset='parcel_id').set_index('parcel_id')['plan_type_id']
    mask = all_df['parcel_id'].isin(parcels_to_update['parcel_id'])
    all_df.loc[mask, 'plan_type_id'] = all_df.loc[mask, 'parcel_id'].map(parcel_to_new_ptid)

    # add _hb_transit to juris_zn for parcels in HB 1110 transit zones (tier 1 or 2 and transit=1) that are residential or mixed-use
    all_df.loc[
        (all_df[pin_name].isin(hb_transit_parcels)) & 
        (all_df['hb_1110_tier'].isin([1,2])) &
        ((all_df['Res_Use'] == 1) | (all_df['Mixed_Use'] == 1))
        ,'juris_zn'] = all_df.loc[
            (all_df[pin_name].isin(hb_transit_parcels)) & 
            (all_df['hb_1110_tier'].isin([1,2])) &
            ((all_df['Res_Use'] == 1) | (all_df['Mixed_Use'] == 1))
            ,'juris_zn'].apply(lambda x: x + '_hb_transit' if pd.notnull(x) else x)

    # update plan_type_id for parcels in HB 1110 transit zones
    hb_transit_plan_types = f.loc[f['hb_transit_override'] == 1, ['juris_zn','plan_type_id']].set_index('juris_zn')['plan_type_id']
    all_df['plan_type_id'] = all_df['juris_zn'].map(hb_transit_plan_types).fillna(all_df['plan_type_id'])

    #### create development constraints table------------------------------------------------------------------
    # divide lot coverage by 100 to convert from percentage to proportion
    for lc_col in ['LC_Res', 'LC_Office', 'LC_Comm', 'LC_Indust', 'LC_Mixed']:
        f[lc_col] = f[lc_col] / 100

    # unroll constraints from plan_type
    id_cols = ['plan_type_id', 'generic_land_use_type_id', 'constraint_type']


    # --- Residential SF/MF unroll -----------------------------------------------
    # Logic:
    #   * If SingleFamily_Use / MultiFamily_Use flags are populated ('Y'), use them.
    #   * If both flags are NA, fall back to the old density-based logic:
    #       - MaxDU_Res < 11.9  -> SF
    #       - MaxDU_Res >= 11.9  -> MF

    res        = f['Res_Use'] == 1
    flags_na   = f['SingleFamily_Use'].isna() & f['MultiFamily_Use'].isna()
    sf_flagged = (f['SingleFamily_Use'] == 'Y') & (f['MultiFamily_Use'] != 'Y')  # if both flags are Y, default to MF
    mf_flagged = (f['MultiFamily_Use'] == 'Y')

    res_cols   = ['MinDU_Res', 'MaxDU_Res', 'LC_Res', 'MaxHt_Res']
    res_rename = {'MinDU_Res': 'minimum', 'MaxDU_Res': 'maximum',
                'LC_Res': 'lc', 'MaxHt_Res': 'maxht'}

    def _build_res(mask, glu_id):
        out = f.loc[mask].copy()
        out['generic_land_use_type_id'] = glu_id
        out['constraint_type'] = 'units_per_acre'
        return out[id_cols + res_cols].rename(columns=res_rename)

    # --- SF ---
    sf_old_mask = res & flags_na & (f['MaxDU_Res'] < 35.1)
    sf_new_mask = res & sf_flagged
    sf_old = _build_res(sf_old_mask, 1)
    sf_new = _build_res(sf_new_mask, 1)
    sf = pd.concat([sf_old, sf_new], ignore_index=True)
    print(f"SF (old/density-only, flags NA & MaxDU_Res < 35.1): {len(sf_old)}")
    print(f"SF (new flag, SingleFamily_Use == 'Y'):              {len(sf_new)}")
    print(f"SF total rows going into devconstr:                  {len(sf)}")

    # --- MF ---
    mf_old_mask = res & flags_na & (f['MaxDU_Res'] > 11.9)
    mf_new_mask = res & mf_flagged
    mf_old = _build_res(mf_old_mask, 2)
    mf_new = _build_res(mf_new_mask, 2)
    mf = pd.concat([mf_old, mf_new], ignore_index=True)
    print(f"MF (old/density-only, flags NA & MaxDU_Res > 11.9):  {len(mf_old)}")
    print(f"MF (new flag, MultiFamily_Use == 'Y'):               {len(mf_new)}")
    print(f"MF total rows going into devconstr:                  {len(mf)}")

    # --- Diagnostics (optional) ---
    print(f"Res_Use==1 rows captured by none of the four subsets: "
        f"{int((res & ~(sf_old_mask | mf_old_mask | sf_new_mask | mf_new_mask)).sum())}")

    # --- Office ---
    off = f[(f['Office_Use'] == 1)]
    off['generic_land_use_type_id'] = 3
    off['constraint_type'] = 'far'
    off = off[id_cols + ['MinFAR_Office', 'MaxFAR_Office', 'LC_Office', 'MaxHt_Office']]
    off = off.rename(columns = {'MinFAR_Office': 'minimum', 'MaxFAR_Office': 'maximum', 'LC_Office':'lc', 'MaxHt_Office':'maxht'})

    # comm
    comm = f[(f['Comm_Use'] == 1)]
    comm['generic_land_use_type_id'] = 4
    comm['constraint_type'] = 'far'
    comm = comm[id_cols + ['MinFAR_Comm', 'MaxFAR_Comm', 'LC_Comm', 'MaxHt_Comm']]
    comm = comm.rename(columns = {'MinFAR_Comm': 'minimum', 'MaxFAR_Comm': 'maximum', 'LC_Comm':'lc', 'MaxHt_Comm':'maxht'})

    # ind
    ind = f[(f['Indust_Use'] == 1)]
    ind['generic_land_use_type_id'] = 5
    ind['constraint_type'] = 'far'
    ind = ind[id_cols + ['MinFAR_Indust', 'MaxFAR_Indust', 'LC_Indust', 'MaxHt_Indust']]
    ind = ind.rename(columns = {'MinFAR_Indust': 'minimum', 'MaxFAR_Indust': 'maximum', 'LC_Indust':'lc', 'MaxHt_Indust':'maxht'})

    # mixed
    mixed = f[(f['Mixed_Use'] == 1)]
    mixed['generic_land_use_type_id'] = 6
    mixed['constraint_type'] = 'far'
    mixed = mixed[id_cols + ['MinFAR_Mixed', 'MaxFAR_Mixed', 'LC_Mixed', 'MaxHt_Mixed']]
    mixed = mixed.rename(columns = {'MinFAR_Mixed': 'minimum', 'MaxFAR_Mixed': 'maximum', 'LC_Mixed':'lc', 'MaxHt_Mixed':'maxht'})

    # mixed du
    mixed_du = f[(f['Mixed_Use'] == 1)]
    mixed_du['generic_land_use_type_id'] = 6
    mixed_du['constraint_type'] = 'units_per_acre'
    mixed_du = mixed_du[id_cols + ['MinDU_Res', 'MaxDU_Res', 'LC_Mixed', 'MaxHt_Mixed']]
    mixed_du = mixed_du.rename(columns = {'MinDU_Res': 'minimum', 'MaxDU_Res': 'maximum', 'LC_Mixed':'lc', 'MaxHt_Mixed':'maxht'})

    # sf du per lot
    sf_du_lot = f[(f['Res_Use'] == 1) & (f['FloorMaxDU_lot'] > 0) & (f['FloorMaxDU_lot'] <= 2)].copy()
    sf_du_lot['generic_land_use_type_id'] = 1
    sf_du_lot['constraint_type'] = 'units_per_lot'
    sf_du_lot['MinDU_lot'] = sf_du_lot['MinDU_lot'].fillna(2)  # default min for SF
    sf_du_lot = sf_du_lot[id_cols + ['MinDU_lot','FloorMaxDU_lot', 'LC_Res', 'MaxHt_Res']]
    sf_du_lot = sf_du_lot.rename(columns = {'MinDU_lot': 'minimum', 'FloorMaxDU_lot': 'maximum', 'LC_Res':'lc', 'MaxHt_Res':'maxht'})

    # mf du per lot
    mf_du_lot = f[(f['Res_Use'] == 1) & (f['FloorMaxDU_lot'] > 2)].copy()
    mf_du_lot['generic_land_use_type_id'] = 2
    mf_du_lot['constraint_type'] = 'units_per_lot'
    mf_du_lot['MinDU_lot'] = mf_du_lot['MinDU_lot'].fillna(3)  # default min for MF
    mf_du_lot = mf_du_lot[id_cols + ['MinDU_lot','FloorMaxDU_lot', 'LC_Res', 'MaxHt_Res']]
    mf_du_lot = mf_du_lot.rename(columns = {'MinDU_lot': 'minimum', 'FloorMaxDU_lot': 'maximum', 'LC_Res':'lc', 'MaxHt_Res':'maxht'})

    # combine together and add lockouts
    lockout_id = 9999
    devconstr = pd.concat([
        sf, 
        mf, 
        off, 
        comm, 
        ind, 
        mixed, 
        mixed_du, 
        sf_du_lot, 
        mf_du_lot
    ], sort=False)

    # clamp minimum to maximum when minimum > maximum (ignore NaNs)
    _min_gt_max = devconstr['minimum'].notna() & devconstr['maximum'].notna() & (devconstr['minimum'] > devconstr['maximum'])
    print(f"Rows where minimum > maximum (clamped to maximum): {int(_min_gt_max.sum())}")
    devconstr.loc[_min_gt_max, 'minimum'] = devconstr.loc[_min_gt_max, 'maximum']

    ## consistency check (ptids)
    ptid_qc_dir = os.path.join(OUTPUT, "ptid_qc")
    os.makedirs(ptid_qc_dir, exist_ok = True)

    common = f.merge(devconstr,on=['plan_type_id','plan_type_id'])
    not_in_devconstr = f.loc[(~f.plan_type_id.isin(common.plan_type_id)), ['plan_type_id', 'FLU_master_id', 'juris_zn']]
    # The reason for being in f but not devcostr is that they did not fit into any of the categories above (sf, mf, com ...).
    # This could happen if instead of "Yes" in the use column, these records have some text.
    # After reviewing these records we decided to lock them out.
    print('WARNING: The following ptids are in object f but not devconstr:\n')
    print(not_in_devconstr)
    not_in_devconstr.to_csv(os.path.join(ptid_qc_dir, r'ptid_consistency_qc_notindevconstr_' + str(date.today()) + '.csv'), index=False)


    max_zero_devconstr = devconstr.groupby(["plan_type_id"]).maximum.sum().reset_index()
    max_zero = max_zero_devconstr[max_zero_devconstr['maximum'] == 0]
    print('The following are non-9*** lockout plan types')
    print(max_zero)
    max_zero.to_csv(os.path.join(ptid_qc_dir, r'ptid_consistency_qc_maxzero_' + str(date.today()) + '.csv'), index=False)

    # create df of plan_type_id 9999
    lockout_df = pd.DataFrame({'plan_type_id': np.repeat(lockout_id, 7),
                'generic_land_use_type_id': list(np.arange(1, 7)) + [6],
                'minimum': 0,
                'maximum': 0,
                'lc': 1,
                'constraint_type': list(np.repeat("units_per_acre", 2)) + list(np.repeat("far", 4)) + ["units_per_acre"]})

    devconstr = pd.concat([devconstr, lockout_df], sort=False)

    # replace NA with 0, or 1 for Lot Coverage (lc)
    devconstr.loc[devconstr['minimum'].isnull(), 'minimum'] = 0
    devconstr.loc[devconstr['maximum'].isnull(), 'maximum'] = 0
    devconstr.loc[devconstr['lc'].isnull(), 'lc'] = 1
    devconstr.loc[devconstr['maxht'].isnull(), 'maxht'] = 0

    # add an id column
    devconstr['development_constraint_id']= np.arange(len(devconstr)) + 1

    # export files ---------------------------------------------------------------------
    res_constr_dir = os.path.join(OUTPUT, "dev_constraints")
    os.makedirs(res_constr_dir, exist_ok = True)

    res_flu_dir = os.path.join(OUTPUT, "flu")
    os.makedirs(res_flu_dir, exist_ok = True)

    devconstr.to_csv(os.path.join(res_constr_dir, r'devconstr_no_lockouts_' + str(date.today()) + '.csv'), index=False) 
    f.to_csv(os.path.join(res_flu_dir, r'flu_imputed_ptid_' + str(date.today()) + '.csv'), index=False) # flu imputed kitchen sink file

    prcls_flu_ptid = all_df[[pin_name, 'plan_type_id', 'tod_id']]
    prcls_flu_ptid.to_csv(os.path.join(res_constr_dir, r'prcls_ptid_no_lockouts_' + str(date.today()) + '.csv'), index=False)
    #all_df.to_file(os.path.join(OUTPUT, r'shapes\prclpt18_ptid_' + str(date.today()) + '.shp')) # Warning! Takes a long time to write!

    #### post-processing lockouts ----------------------------------------------------------

    # append to development constraints
    lo_df = pd.DataFrame()
    for x in range(9001, 9008):
        lockout_ptid_df = pd.DataFrame({'plan_type_id': np.repeat(x, 7),
                    'generic_land_use_type_id': list(np.arange(1, 7)) + [6],
                    'minimum': 0,
                    'maximum': 0,
                    'lc': 1,
                    'constraint_type': list(np.repeat("units_per_acre", 2)) + list(np.repeat("far", 4)) + ["units_per_acre"],
                    'maxht': 0})
        if lo_df.empty:
            lo_df = lockout_ptid_df
        else:
            lo_df = pd.concat([lo_df, lockout_ptid_df])

    dci = devconstr['development_constraint_id'].max()
    lo_df['development_constraint_id'] = list(np.arange(dci+1, dci+len(lo_df)+1))
    devconstr = pd.concat([devconstr, lo_df]) 

    # update plan_type_ids
    all_df.loc[all_df['plan_type_id'].isnull(), 'plan_type_id'] = lockout_id
    all_df.loc[all_df['plan_type_id'].isin(not_in_devconstr['plan_type_id']), 'plan_type_id'] = lockout_id  # lock plan types not found in devconstr
    all_df.loc[all_df['lu_type'] == 23, 'plan_type_id'] = 9001 # Schools/universities
    all_df.loc[all_df['lu_type'] == 7, 'plan_type_id'] = 9002 # Government
    all_df.loc[all_df['lu_type'] == 9, 'plan_type_id'] = 9003 # Hospitals, convalescent center
    all_df.loc[all_df['lu_type'] == 6, 'plan_type_id'] = 9004 # Forest, protected
    all_df.loc[all_df['lu_type'] == 5, 'plan_type_id'] = 9005 # Forest, harvestable
    all_df.loc[all_df['lu_type'] == 1, 'plan_type_id'] = 9006 # Agriculture
    all_df.loc[all_df['lu_type'] == 27, 'plan_type_id'] = 9007 # Vacant undevelopable

    # export post-processing lockouts version
    prcls_flu_ptid_lockouts = all_df[[pin_name, 'plan_type_id', 'tod_id']]
    prcls_flu_ptid_lockouts.to_csv(os.path.join(res_constr_dir, r'prcls_ptid_final_' + str(date.today()) + '.csv'), index=False)
    devconstr.to_csv(os.path.join(res_constr_dir, r'devconstr_final_' + str(date.today()) + '.csv'), index=False)

    # QC check on number of parcels with missing FLU match (plan_type_id 9999)
    (all_df[all_df['plan_type_id'] == 9999]
        .groupby('juris_zn').size().reset_index(name='num_parcels')
        .sort_values(by='num_parcels', ascending=False)
        .to_csv(os.path.join(OUTPUT,'flu_qc', r'parcels_no_table_match_' + str(date.today()) + '.csv'), index=False)
    )
    print(f"Number of parcels with missing FLU match: {len(all_df[all_df['plan_type_id'] == 9999])} parcels will be assigned plan type id 9999")

    #---------------------------------------------------------------------------------------------------
    # HB1110 analysis

    parcel_df = (
        all_df[['parcel_id','gross_sqft','plan_type_id']]
        .merge(f, on='plan_type_id', how='left')
        .query(
            '(plan_type_id < 9000) & (Res_Use == 1) & (gross_sqft > 0)'
        )
    )

    parcel_df['gross_acres'] = parcel_df['gross_sqft'] / 43560
    parcel_df['dua_units'] = (parcel_df['MaxDU_Res'] * parcel_df['gross_acres']).round(0)
    parcel_df['du_lot_units'] = parcel_df['FloorMaxDU_lot'].round(0)

    hb_sf = (parcel_df.loc[
        (parcel_df['SingleFamily_Use'] == 'Y') & (parcel_df['MultiFamily_Use'].isna())]
        .groupby([
            'Juris','hb_1110_tier','hb_transit_override'],dropna=False)
            [['dua_units','du_lot_units']].sum()
    )
    hb_mf = (parcel_df.loc[
        (parcel_df['MultiFamily_Use'] == 'Y') & (parcel_df['SingleFamily_Use'].isna())]
        .groupby([
            'Juris','hb_1110_tier','hb_transit_override'],dropna=False)
            [['dua_units','du_lot_units']].sum()
    )
    hb = hb_sf.merge(hb_mf, left_index=True, right_index=True, how='outer', suffixes=('_sf','_mf')).reset_index()
    hb.sort_values(by=['hb_1110_tier','Juris','hb_transit_override']).to_csv(os.path.join(OUTPUT,'flu_qc', r'flu_hb1110_summary_' + str(date.today()) + '.csv'), index=False)
    return context