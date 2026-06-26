import geopandas as gpd
import numpy as np
import os
import pandas as pd
from datetime import date
from itertools import combinations
from future_land_use.util import Pipeline


COUNTY_JURIS_ZNS = [
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


# ----- helper functions -----------------------------------------------------------

def check_multi_pins(prcls_flu, out_dir, pin_name='PIN'):
    today = pd.to_datetime('today').date()
    # prcls_flu is the spatial join of parcels to the FLU

    # count number of ptids per parcel
    pin_cnt = prcls_flu.groupby([pin_name])['plan_type_id'].count().reset_index()
    pin_cnt = pin_cnt.rename(columns={'plan_type_id': 'ptid_count'})

    pins_multi = pin_cnt[pin_cnt['ptid_count'] > 1]

    print(pins_multi)
    print('Number of unique parcel_ids affected by overlapping FLU polygons: ' + str(len(pins_multi)))

    if (len(pins_multi) > 0):
        # export list of parcels that overlay stacked flu polygons
        out_dir = os.path.join(out_dir, "flu_qc")
        os.makedirs(out_dir, exist_ok=True)
        pins_multi.to_csv(os.path.join(out_dir, r'pins_multi_' + str(today) + '.csv'), index=False)
        # export point shapefile of where overlapping zones occur for GIS staff to reconcile
        prcls_multi_ptid = prcls_flu[prcls_flu[pin_name].isin(pins_multi[pin_name].tolist())]
        prcls_multi_ptid = prcls_multi_ptid[[pin_name, 'geometry', 'juris_zn', 'plan_type_id']]
        prcls_multi_ptid.to_file(os.path.join(out_dir, 'prcls_multi_ptid.gdb'), layer=f'prcls_multi_ptid_{today}', driver='OpenFileGDB')
        print('exported list of multi ptid instances as csv and gdb layer to ' + out_dir)


def _build_flu_with_plan_types(p, cfg):
    """Load imputed FLU data (with or without HB 1110), assign plan_type_id, merge to shapefile."""
    if cfg.get('apply_hb_1110', False):
        flu_table = 'flu_imputed_hb_1110'
    else:
        flu_table = 'flu_imputed'
    f = p.get_table(flu_table)

    # created plan_type_id for each juris_zn in the imputed FLU table
    print(f"Creating {len(f)} plan_type_id values for {flu_table}...")
    f['plan_type_id'] = np.arange(len(f)) + 1
    p.save_table(f,flu_table)

    flu_shp = p.get_geodataframe('flu_shp')
    flu = flu_shp.merge(f, on=['juris_zn'], how='left')
    return flu


def _load_parcels_with_land_use(p, pin_name):
    """Load parcel points, cast pin to int64, merge with land-use type table."""
    print("Reading in parcels...")
    prcls = p.get_geodataframe('parcels_pts')[['parcel_id', 'geometry']]
    prcls[pin_name] = prcls[pin_name].astype(np.int64)
    lu_type = p.get_table('parcels_land_use_type')
    prcls = prcls.merge(lu_type, on=pin_name)
    return prcls


def _spatial_join_parcels_to_flu(prcls, flu):
    """Left sjoin parcels → flu; fall back to sjoin_nearest for unmatched; flag no_flu_match."""
    print("Spatially joining parcels to FLU shapefile...")
    prcls_flu = gpd.sjoin(prcls, flu, how='left')

    unmatched = prcls_flu.loc[
        prcls_flu['juris_zn'].isna(),
        ['parcel_id', 'geometry', 'lu_type', 'tod_id', 'gross_sqft']
    ].copy()

    prct_unmatched = len(unmatched) / len(prcls)
    print(f"Number of parcels with no spatial match to FLU shp: {len(unmatched)} "
          f"out of {len(prcls)} total parcels ({prct_unmatched:.1%}). "
          f"Unmatched parcels will be joined to nearest FLU polygon.")

    if prct_unmatched > 0.05:
        raise RuntimeError(
            f"Warning: {len(unmatched)} parcels have no spatial match to FLU shapefile, "
            f"which is more than 5% of total parcels. Check data and spatial join parameters."
        )

    unmatched_flu = gpd.sjoin_nearest(unmatched, flu)
    prcls_flu = prcls_flu.loc[~prcls_flu['juris_zn'].isna()].copy()
    prcls_flu = pd.concat([prcls_flu, unmatched_flu], ignore_index=True)

    prcls_flu['no_flu_match'] = 0
    prcls_flu.loc[prcls_flu['plan_type_id'].isna(), 'no_flu_match'] = 1
    return prcls_flu


def _deduplicate_parcel_flu_matches(prcls, prcls_flu, pin_name):
    """Deduplicate parcels that matched multiple FLU zones.
    
    Prioritises city-level juris_zn over county-level (county rows sorted last,
    then keep='first'). Handles double and triple+ matches separately.
    """
    prcls_flu[pin_name].duplicated().any()  # diagnostic
    duplicate = prcls_flu[prcls_flu.duplicated(pin_name)][[pin_name]]
    dup_df = prcls_flu[prcls_flu[pin_name].isin(duplicate[pin_name])].sort_values(by=[pin_name])

    dup_pin_freq = dup_df.groupby([pin_name])[pin_name].count().reset_index(name='counts')
    triple_pin = dup_pin_freq[dup_pin_freq['counts'] > 2]

    unjoined = prcls[~prcls[pin_name].isin(prcls_flu[pin_name])]
    x1 = prcls_flu[~prcls_flu[pin_name].isin(dup_df[pin_name])]

    x2 = dup_df[
        ~dup_df['plan_type_id'].isnull() &
        ~dup_df[pin_name].isin(triple_pin[pin_name])
    ]
    x2a = dup_df[
        dup_df[pin_name].isin(triple_pin[pin_name]) &
        ~dup_df['plan_type_id'].isnull()
    ]

    def _keep_first_non_county(df):
        """Sort county rows last within each pin group, then drop_duplicates keep='first'."""
        df['_is_county'] = df['juris_zn'].str.lower().str.startswith(tuple(COUNTY_JURIS_ZNS))
        df = df.sort_values(by=[pin_name, '_is_county'])
        return df.drop_duplicates(subset=[pin_name], keep='first').drop(columns=['_is_county'])

    x2_kp_first = _keep_first_non_county(x2)
    x2a_kp_first = _keep_first_non_county(x2a)

    _ = len(prcls) - (len(x1) + len(unjoined) + len(x2_kp_first) + len(x2a_kp_first))  # diagnostic

    all_df = pd.concat([x1, x2_kp_first, x2a_kp_first, unjoined])
    return all_df, dup_df


def _export_juris_pair_counts(dup_df, pin_name, output_dir):
    """Count juris_zn pairs that co-occur on the same parcel and export to CSV."""
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
    pair_counts.to_csv(
        os.path.join(output_dir, "flu_qc", f'flu_juris_zn_pair_counts_{date.today()}.csv'),
        index=False,
    )


# ----- main step ------------------------------------------------------------------


def run_step(context):
    print("Running step: parcel_flu_spatial_join...")
    # ---- configs
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings.get('unroll_constraints_settings', {})
    ROOT = cfg['root_dir']
    OUTPUT = os.path.join(ROOT, "unroll_constraints")

    pin_name = cfg['parcel_id_col']

    # -- build FLU with plan_type_id
    flu = _build_flu_with_plan_types(p, cfg)

    # -- load parcels
    prcls = _load_parcels_with_land_use(p, pin_name)

    # -- spatial join
    prcls_flu = _spatial_join_parcels_to_flu(prcls, flu)

    # -- QC & deduplicate
    check_multi_pins(prcls_flu, OUTPUT, pin_name=pin_name)
    all_df, dup_df = _deduplicate_parcel_flu_matches(prcls, prcls_flu, pin_name)

    # -- QC: juris pair co-occurrence
    _export_juris_pair_counts(dup_df, pin_name, OUTPUT)

    # -- save output
    df_out = all_df[[pin_name, 'juris_zn', 'plan_type_id']]
    p.save_table(df_out, 'parcel_plan_type_xwalk')