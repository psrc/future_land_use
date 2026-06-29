import numpy as np
import os
import pandas as pd

from future_land_use.util.pipeline import Pipeline

ID_COLS = ['plan_type_id', 'generic_land_use_type_id', 'constraint_type']
LC_COLS = ['LC_Res', 'LC_Office', 'LC_Comm', 'LC_Indust', 'LC_Mixed']


# ---------------------------------------------------------------------------
# helper: lockout-row template
# ---------------------------------------------------------------------------
def _make_lockout_rows(plan_type_id, include_maxht=False):
    """Return a 7-row DataFrame of zeroed-out constraints for a lockout
    plan_type_id.  If *include_maxht* is True, a 'maxht'=0 column is added."""
    df = pd.DataFrame({
        'plan_type_id': np.repeat(plan_type_id, 7),
        'generic_land_use_type_id': list(np.arange(1, 7)) + [6],
        'minimum': 0,
        'maximum': 0,
        'lc': 1,
        'constraint_type': (
            list(np.repeat("units_per_acre", 2))
            + list(np.repeat("far", 4))
            + ["units_per_acre"]
        ),
    })
    if include_maxht:
        df['maxht'] = 0
    return df


# ---------------------------------------------------------------------------
# helper: non-residential FAR / DUA unroll
# ---------------------------------------------------------------------------
def _unroll_far_or_dua(f, use_col, glu_id, constraint_type,
                       min_col, max_col, lc_col, ht_col):
    """Generic unroll for office/comm/ind/mixed FAR or mixed DUA."""
    df = f.loc[f[use_col] == 1].copy()
    df['generic_land_use_type_id'] = glu_id
    df['constraint_type'] = constraint_type
    df = df[ID_COLS + [min_col, max_col, lc_col, ht_col]]
    return df.rename(columns={
        min_col: 'minimum', max_col: 'maximum',
        lc_col: 'lc', ht_col: 'maxht',
    })


# ---------------------------------------------------------------------------
# helper: DU-per-lot unroll
# ---------------------------------------------------------------------------
def _unroll_du_lot(f, floor_col, glu_id, default_min, lc_col, ht_col, max_floor):
    """Unroll SF or MF units-per-lot constraints (filtered by *max_floor*)."""
    mask = (f['Res_Use'] == 1) & (f[floor_col] > 0) & (f[floor_col] <= max_floor)
    df = f.loc[mask].copy()
    df['generic_land_use_type_id'] = glu_id
    df['constraint_type'] = 'units_per_lot'
    df['MinDU_lot'] = df['MinDU_lot'].fillna(default_min)
    min_col, lc, ht = 'MinDU_lot', lc_col, ht_col
    df = df[ID_COLS + [min_col, floor_col, lc, ht]]
    return df.rename(columns={
        min_col: 'minimum', floor_col: 'maximum', lc: 'lc', ht: 'maxht',
    })


# ---------------------------------------------------------------------------
# helper: residential SF/MF unroll
# ---------------------------------------------------------------------------
def _unroll_residential(f):
    """Unroll residential constraints into SF and MF DataFrames.
    Returns (sf, mf)."""
    res = f['Res_Use'] == 1
    flags_na = f['SingleFamily_Use'].isna() & f['MultiFamily_Use'].isna()
    sf_flagged = (f['SingleFamily_Use'] == 'Y') & (f['MultiFamily_Use'] != 'Y')
    mf_flagged = (f['MultiFamily_Use'] == 'Y')

    res_cols = ['MinDU_Res', 'MaxDU_Res', 'LC_Res', 'MaxHt_Res']
    res_rename = {'MinDU_Res': 'minimum', 'MaxDU_Res': 'maximum',
                  'LC_Res': 'lc', 'MaxHt_Res': 'maxht'}

    def _build_res(mask, glu_id):
        out = f.loc[mask].copy()
        out['generic_land_use_type_id'] = glu_id
        out['constraint_type'] = 'units_per_acre'
        return out[ID_COLS + res_cols].rename(columns=res_rename)

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

    # --- Diagnostics ---
    print(
        f"Res_Use==1 rows captured by none of the four subsets: "
        f"{int((res & ~(sf_old_mask | mf_old_mask | sf_new_mask | mf_new_mask)).sum())}"
    )

    return sf, mf


# ---------------------------------------------------------------------------
# helper: lockout plan_type_id assignment
# ---------------------------------------------------------------------------
_LU_TYPE_LOCKOUT_MAP = {
    23: 9001,  # Schools/universities
    7:  9002,  # Government
    9:  9003,  # Hospitals, convalescent center
    6:  9004,  # Forest, protected
    5:  9005,  # Forest, harvestable
    1:  9006,  # Agriculture
    27: 9007,  # Vacant undevelopable
}


def _apply_lockout_plan_types(all_df, not_in_devconstr, lockout_id):
    """Update plan_type_id in *all_df* for nulls, missing devconstr matches,
    and land-use-type-based lockouts."""
    all_df.loc[all_df['plan_type_id'].isnull(), 'plan_type_id'] = lockout_id
    all_df.loc[
        all_df['plan_type_id'].isin(not_in_devconstr['plan_type_id']),
        'plan_type_id'
    ] = lockout_id
    for lu_type, ptid in _LU_TYPE_LOCKOUT_MAP.items():
        all_df.loc[all_df['lu_type'] == lu_type, 'plan_type_id'] = ptid
    return all_df


# ---------------------------------------------------------------------------
# helper: HB 1110 analysis
# ---------------------------------------------------------------------------
def _run_hb1110_analysis(all_df, f, OUTPUT, today):
    """Produce HB 1110 summary CSV for residential parcels with capacity."""
    parcel_df = (
        all_df[['parcel_id', 'gross_sqft', 'plan_type_id']]
        .merge(f, on='plan_type_id', how='left')
        .query('(plan_type_id < 9000) & (Res_Use == 1) & (gross_sqft > 0)')
    )
    parcel_df['gross_acres'] = parcel_df['gross_sqft'] / 43560
    parcel_df['dua_units'] = (parcel_df['MaxDU_Res'] * parcel_df['gross_acres']).round(0)
    parcel_df['du_lot_units'] = parcel_df['FloorMaxDU_lot'].round(0)

    group_cols = ['Juris', 'hb_1110_tier', 'hb_transit_override']
    val_cols = ['dua_units', 'du_lot_units']

    hb_sf = (
        parcel_df.loc[
            (parcel_df['SingleFamily_Use'] == 'Y') & (parcel_df['MultiFamily_Use'].isna())
        ]
        .groupby(group_cols, dropna=False)[val_cols].sum()
    )
    hb_mf = (
        parcel_df.loc[
            (parcel_df['MultiFamily_Use'] == 'Y') & (parcel_df['SingleFamily_Use'].isna())
        ]
        .groupby(group_cols, dropna=False)[val_cols].sum()
    )
    hb = hb_sf.merge(hb_mf, left_index=True, right_index=True,
                     how='outer', suffixes=('_sf', '_mf')).reset_index()
    hb.sort_values(by=['hb_1110_tier', 'Juris', 'hb_transit_override']).to_csv(
        os.path.join(OUTPUT, 'flu_qc', 'flu_hb1110_summary_' + str(today) + '.csv'),
        index=False,
    )



# ===================================================================
def run_step(context):
    print("Running step: unroll_constraints...")
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings.get('unroll_constraints_settings', {})
    ROOT = cfg['root_dir']
    OUTPUT = os.path.join(ROOT, "unroll_constraints")
    today = pd.to_datetime("today").date()
    pin_name = cfg['parcel_id_col']

    if cfg.get('apply_hb_1110', False):
        f = p.get_table('flu_imputed_hb_1110_with_overlays')
        all_df = p.get_table('parcel_plan_type_xwalk_with_overlays_hb_1110')
    else:
        f = p.get_table('flu_imputed_with_overlays')
        all_df = p.get_table('parcel_plan_type_xwalk_with_overlays')

    parcels_land_use = p.get_table('parcels_land_use_type')
    all_df = all_df.merge(parcels_land_use, on='parcel_id', how='left')

    # ---- lot coverage: percent → proportion ----
    for lc_col in LC_COLS:
        f[lc_col] = f[lc_col] / 100

    # ---- unroll constraints ----
    sf, mf = _unroll_residential(f)
    off    = _unroll_far_or_dua(f, 'Office_Use', 3, 'far',
                                'MinFAR_Office', 'MaxFAR_Office',
                                'LC_Office', 'MaxHt_Office')
    comm   = _unroll_far_or_dua(f, 'Comm_Use', 4, 'far',
                                'MinFAR_Comm', 'MaxFAR_Comm',
                                'LC_Comm', 'MaxHt_Comm')
    ind    = _unroll_far_or_dua(f, 'Indust_Use', 5, 'far',
                                'MinFAR_Indust', 'MaxFAR_Indust',
                                'LC_Indust', 'MaxHt_Indust')
    mixed  = _unroll_far_or_dua(f, 'Mixed_Use', 6, 'far',
                                'MinFAR_Mixed', 'MaxFAR_Mixed',
                                'LC_Mixed', 'MaxHt_Mixed')
    mixed_du = _unroll_far_or_dua(f, 'Mixed_Use', 6, 'units_per_acre',
                                  'MinDU_Res', 'MaxDU_Res',
                                  'LC_Mixed', 'MaxHt_Mixed')

    sf_du_lot = _unroll_du_lot(f, 'FloorMaxDU_lot', 1, 2, 'LC_Res', 'MaxHt_Res', 2)
    mf_du_lot = _unroll_du_lot(f, 'FloorMaxDU_lot', 2, 3, 'LC_Res', 'MaxHt_Res', 9999)

    # ---- combine ----
    lockout_id = 9999
    devconstr = pd.concat([
        sf, mf, off, comm, ind, mixed, mixed_du, sf_du_lot, mf_du_lot,
    ], sort=False)

    # ---- clamp minimum < maximum ----
    devconstr['minimum'] = devconstr['minimum'].fillna(0)
    devconstr['maximum'] = devconstr['maximum'].fillna(0)
    
    _min_gt_max = (
        devconstr['minimum'].notna() & devconstr['maximum'].notna()
        & (devconstr['minimum'] > devconstr['maximum'])
    )
    print(f"Rows where minimum > maximum: {int(_min_gt_max.sum())}")
    devconstr.loc[_min_gt_max, 'minimum'] = devconstr.loc[_min_gt_max, 'maximum']

    # ---- consistency check (ptids) ----
    ptid_qc_dir = os.path.join(OUTPUT, "ptid_qc")
    os.makedirs(ptid_qc_dir, exist_ok=True)

    common = f.merge(devconstr, on=['plan_type_id', 'plan_type_id'])
    not_in_devconstr = f.loc[
        ~f.plan_type_id.isin(common.plan_type_id),
        ['plan_type_id', 'FLU_master_id', 'juris_zn']
    ]
    print('WARNING: The following ptids are in object f but not devconstr:\n')
    print(not_in_devconstr)
    not_in_devconstr.to_csv(
        os.path.join(ptid_qc_dir, 'ptid_consistency_qc_notindevconstr_' + str(today) + '.csv'),
        index=False,
    )

    max_zero_devconstr = devconstr.groupby("plan_type_id")['maximum'].sum().reset_index()
    max_zero = max_zero_devconstr[max_zero_devconstr['maximum'] == 0]
    print('The following are non-9*** lockout plan types')
    print(max_zero)
    max_zero.to_csv(
        os.path.join(ptid_qc_dir, 'ptid_consistency_qc_maxzero_' + str(today) + '.csv'),
        index=False,
    )

    # ---- add lockout rows & finalize ----
    lockout_df = _make_lockout_rows(lockout_id)
    devconstr = pd.concat([devconstr, lockout_df], sort=False)

    devconstr['minimum'] = devconstr['minimum'].fillna(0)
    devconstr['maximum'] = devconstr['maximum'].fillna(0)
    devconstr['lc'] = devconstr['lc'].fillna(1)
    devconstr['maxht'] = devconstr['maxht'].fillna(0)
    devconstr['development_constraint_id'] = np.arange(len(devconstr)) + 1

    # ---- export pre-lockout files ----
    res_constr_dir = os.path.join(OUTPUT, "dev_constraints")
    os.makedirs(res_constr_dir, exist_ok=True)
    res_flu_dir = os.path.join(OUTPUT, "flu")
    os.makedirs(res_flu_dir, exist_ok=True)

    devconstr.to_csv(
        os.path.join(res_constr_dir, 'devconstr_no_lockouts_' + str(today) + '.csv'),
        index=False,
    )
    f.to_csv(
        os.path.join(res_flu_dir, 'flu_imputed_ptid_' + str(today) + '.csv'),
        index=False,
    )
    prcls_flu_ptid = all_df[[pin_name, 'plan_type_id', 'tod_id']]
    prcls_flu_ptid.to_csv(
        os.path.join(res_constr_dir, 'prcls_ptid_no_lockouts_' + str(today) + '.csv'),
        index=False,
    )

    # ---- post-processing lockouts ----
    lo_parts = [_make_lockout_rows(x, include_maxht=True) for x in range(9001, 9008)]
    lo_df = pd.concat(lo_parts, ignore_index=True)
    dci = devconstr['development_constraint_id'].max()
    lo_df['development_constraint_id'] = list(np.arange(dci + 1, dci + len(lo_df) + 1))
    devconstr = pd.concat([devconstr, lo_df], ignore_index=True)

    all_df = _apply_lockout_plan_types(all_df, not_in_devconstr, lockout_id)

    # ---- export final ----
    prcls_flu_ptid_lockouts = all_df[[pin_name, 'plan_type_id', 'tod_id']]
    prcls_flu_ptid_lockouts.to_csv(
        os.path.join(res_constr_dir, 'prcls_ptid_final_' + str(today) + '.csv'),
        index=False,
    )
    devconstr.to_csv(
        os.path.join(res_constr_dir, 'devconstr_final_' + str(today) + '.csv'),
        index=False,
    )

    # ---- QC: parcels with missing FLU match ----
    (
        all_df[all_df['plan_type_id'] == 9999]
        .groupby('juris_zn').size()
        .reset_index(name='num_parcels')
        .sort_values(by='num_parcels', ascending=False)
        .to_csv(
            os.path.join(OUTPUT, 'flu_qc', 'parcels_no_table_match_' + str(today) + '.csv'),
            index=False,
        )
    )
    print(
        f"Number of parcels with missing FLU match: "
        f"{len(all_df[all_df['plan_type_id'] == 9999])} parcels "
        f"will be assigned plan type id 9999"
    )

    # ---- HB 1110 analysis ----
    if cfg.get('apply_hb_1110', False):
        _run_hb1110_analysis(all_df, f, OUTPUT, today)