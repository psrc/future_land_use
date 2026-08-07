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
# helper: du/lot vs du/acre capacity check
# ---------------------------------------------------------------------------
UNLIMITED = -1  # sentinel written by load_FLU2026.R for "unlimited" max density

# max column each use flag activates in the unrolled constraints
USE_MAX_COLS = {
    'Office_Use': 'MaxFAR_Office',
    'Comm_Use': 'MaxFAR_Comm',
    'Indust_Use': 'MaxFAR_Indust',
    'Mixed_Use': 'MaxFAR_Mixed',
}


def _as_capacity(s):
    """Numeric view of a max-density column with the 'unlimited' sentinel mapped to inf."""
    s = pd.to_numeric(s, errors='coerce')
    return s.mask(s == UNLIMITED, np.inf)


def _has_capacity(s):
    """True where a max column is populated and nonzero (unlimited counts as capacity)."""
    c = _as_capacity(s)
    return c.notna() & (c > 0)


def _effective_mixed_du(f):
    """Mixed-use min/max DU, falling back to the residential values when either is missing."""
    fallback = f['MinDU_Mixed'].isna() | f['MaxDU_Mixed'].isna()
    return (
        pd.Series(np.where(fallback, f['MinDU_Res'], f['MinDU_Mixed']), index=f.index),
        pd.Series(np.where(fallback, f['MaxDU_Res'], f['MaxDU_Mixed']), index=f.index),
    )


def _du_lot_only_constraint(f):
    """True where du/lot is the only nonzero max constraint the row's use flags activate."""
    _, max_mixed_du = _effective_mixed_du(f)
    other = (f['Res_Use'] == 1) & _has_capacity(f['MaxDU_Res'])
    other = other | ((f['Mixed_Use'] == 1) & _has_capacity(max_mixed_du))
    for use_col, max_col in USE_MAX_COLS.items():
        other = other | ((f[use_col] == 1) & _has_capacity(f[max_col]))
    return ~other


def _check_du_lot_vs_dua(f, all_df, pin_name, qc_dir, today):
    """Null out FloorMaxDU_lot for plan types whose du/lot capacity never exceeds their
    du/acre capacity on any parcel, so `_unroll_du_lot` skips them.  Returns a copy of *f*."""
    du_lot = _as_capacity(f['FloorMaxDU_lot'])
    _, max_mixed_du = _effective_mixed_du(f)
    dua = pd.concat([
        _as_capacity(f['MaxDU_Res']),
        _as_capacity(max_mixed_du).where(f['Mixed_Use'] == 1),
    ], axis=1).max(axis=1)

    qc = f.loc[
        (f['Res_Use'] == 1) & du_lot.notna() & (du_lot > 0),
        ['plan_type_id', 'juris_zn', 'Juris', 'MaxDU_Res', 'FloorMaxDU_lot'],
    ].copy()
    qc['du_lot_capacity'] = du_lot
    qc['dua_capacity'] = dua
    qc['exempt_only_constraint'] = _du_lot_only_constraint(f)

    parcels = all_df.loc[
        all_df['plan_type_id'].notna()
        & (all_df['plan_type_id'] < 9000)
        & (all_df['gross_sqft'] > 0),
        [pin_name, 'plan_type_id', 'gross_sqft'],
    ].merge(qc[['plan_type_id', 'du_lot_capacity', 'dua_capacity']], on='plan_type_id')

    dua_units = (parcels['dua_capacity'] * (parcels['gross_sqft'] / 43560)).round(0)
    parcels['du_lot_greater'] = parcels['du_lot_capacity'].round(0) > dua_units

    counts = parcels.groupby('plan_type_id').agg(
        n_parcels=('du_lot_greater', 'size'),
        n_du_lot_greater=('du_lot_greater', 'sum'),
    ).reset_index()

    qc = qc.merge(counts, on='plan_type_id', how='left')
    qc[['n_parcels', 'n_du_lot_greater']] = qc[['n_parcels', 'n_du_lot_greater']].fillna(0).astype(int)
    qc['pct_du_lot_greater'] = np.where(
        qc['n_parcels'] > 0, qc['n_du_lot_greater'] / qc['n_parcels'], np.nan
    )
    # a plan type with no du/acre capacity at all can never be beaten by it, so keep du/lot
    qc['no_dua'] = qc['dua_capacity'].isna()
    qc['du_lot_dropped'] = (
        (qc['n_du_lot_greater'] == 0) & ~qc['exempt_only_constraint'] & ~qc['no_dua']
    ).astype(int)

    f_du_lot = f.copy()
    f_du_lot.loc[
        f_du_lot['plan_type_id'].isin(qc.loc[qc['du_lot_dropped'] == 1, 'plan_type_id']),
        'FloorMaxDU_lot',
    ] = np.nan

    qc = qc[[
        'plan_type_id', 'juris_zn', 'Juris', 'MaxDU_Res', 'FloorMaxDU_lot',
        'dua_capacity', 'du_lot_capacity', 'n_parcels', 'n_du_lot_greater',
        'pct_du_lot_greater', 'exempt_only_constraint', 'no_dua', 'du_lot_dropped',
    ]]
    qc.to_csv(os.path.join(qc_dir, 'du_lot_vs_dua_check_' + str(today) + '.csv'), index=False)

    print(f"Plan types with du/lot constraints: {len(qc)}")
    print(f"  never exceed du/acre on any parcel, du/lot dropped: {int(qc['du_lot_dropped'].sum())}")
    print(f"  kept because du/lot is their only constraint:       {int(qc['exempt_only_constraint'].sum())}")
    print(f"  kept because they have no du/acre capacity:         {int(qc['no_dua'].sum())}")
    return f_du_lot


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
    global_cfg = p.settings
    ROOT = global_cfg['root_dir']
    OUTPUT = os.path.join(ROOT, "unroll_constraints")
    flu_qc_dir = os.path.join(OUTPUT, "flu_qc")
    os.makedirs(flu_qc_dir, exist_ok=True)
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

    # ---- drop du/lot constraints that can never bind ----
    if cfg.get('drop_unused_du_lot', False):
        f_du_lot = _check_du_lot_vs_dua(f, all_df, pin_name, flu_qc_dir, today)
    else:
        f_du_lot = f

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
    # use MinDU_Mixed/MaxDU_Mixed when both are populated; otherwise fall
    # back to MinDU_Res/MaxDU_Res
    min_mixed_du, max_mixed_du = _effective_mixed_du(f)
    f_mixed_du = f.assign(
        MinDU_Mixed_eff=min_mixed_du,
        MaxDU_Mixed_eff=max_mixed_du,
    )
    mixed_du = _unroll_far_or_dua(f_mixed_du, 'Mixed_Use', 6, 'units_per_acre',
                                  'MinDU_Mixed_eff', 'MaxDU_Mixed_eff',
                                  'LC_Mixed', 'MaxHt_Mixed')

    sf_du_lot = _unroll_du_lot(f_du_lot, 'FloorMaxDU_lot', 1, 2, 'LC_Res', 'MaxHt_Res', 2)
    mf_du_lot = _unroll_du_lot(f_du_lot, 'FloorMaxDU_lot', 2, 3, 'LC_Res', 'MaxHt_Res', 9999)

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