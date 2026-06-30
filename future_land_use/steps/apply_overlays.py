import numpy as np
import os
import pandas as pd

from future_land_use.util.pipeline import Pipeline

USE_COLS = ['Res_Use', 'Comm_Use', 'Office_Use', 'Indust_Use', 'Mixed_Use']


def _build_parcels_to_update(p, f, all_df):
    """Identify parcels that fall on both an overlay and an underlying zone,
    then assign overlay_combo_id based on unique plan_type_id combinations."""
    overlay_parcels = (
        p.get_table('overlay_parcels')
        .merge(f, on='juris_zn')
        .rename(columns={'juris_zn': 'overlay_juris_zn'})
        [['parcel_id', 'overlay_juris_zn','plan_type_id']]
    )
    parcels_in_overlay = (
        all_df.loc[
            all_df['parcel_id'].isin(overlay_parcels['parcel_id']),
            ['parcel_id', 'juris_zn', 'plan_type_id']]
            .copy().dropna()
    )
    parcels_in_overlay.rename(columns={'juris_zn': 'orig_juris_zn'}, inplace=True)
    parcels_to_update = pd.concat([overlay_parcels, parcels_in_overlay])

    # drop non-duplicates because if there's only 1 parcel_id then it means it must land
    # on an overlay but not another underlying zone
    overlay_only = parcels_to_update.duplicated(subset='parcel_id', keep=False)
    print(f"Parcels that land on an overlay but not another underlying zone: {len(parcels_to_update[~overlay_only])}")
    parcels_to_update = parcels_to_update[overlay_only].copy()

    # find all existing combinations of plan_type_id based on parcel_id
    plan_type_combos = (
        parcels_to_update
        .groupby('parcel_id')['plan_type_id']
        .apply(lambda x: sorted(set(x)))  # sorted unique plan_type_ids per parcel
        .reset_index(name='plan_type_id_combo')
    )
    print(f"Unique parcel_id + plan_type_id combos: {len(plan_type_combos)}")

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
    return parcels_to_update


def _build_overlay_plan_types(parcels_to_update, f, OUTPUT, today):
    """Build new zone rows for each overlay combination by aggregating
    to the minimum values and assigning new plan_type_id and juris_zn."""
    overlay_plan_types = parcels_to_update.drop_duplicates(
        subset=['plan_type_id', 'overlay_combo_id']
    ).drop(columns=['parcel_id'])

    overlay_plan_types_out = overlay_plan_types.merge(f, on='plan_type_id', how='left')

    # Remove the jurisdiction name + underscore from overlay_juris_zn (e.g. "Tacoma_STPG" -> "STPG")
    overlay_plan_types_out['overlay_juris_zn'] = overlay_plan_types_out.apply(
        lambda row: row['overlay_juris_zn'].replace(str(row['Juris']) + '_', '', 1)
        if pd.notna(row['Juris']) and pd.notna(row['overlay_juris_zn'])
        else row['overlay_juris_zn'],
        axis=1
    )

    # replace -1 values in use columns with NaN for aggregation
    overlay_plan_types_out[USE_COLS] = overlay_plan_types_out[USE_COLS].replace(-1, np.nan)

    # aggregate to get minimum values for each overlay_combo_id
    min_cols = USE_COLS + [
        'orig_juris_zn', 'MinDU_Res', 'MinFAR_Comm', 'MinFAR_Office', 'MinFAR_Indust', 'MinFAR_Mixed',
        'MaxDU_Res', 'MaxFAR_Comm', 'MaxFAR_Office', 'MaxFAR_Indust', 'MaxFAR_Mixed', 'MaxHt_Res',
        'MaxHt_Comm', 'MaxHt_Office', 'MaxHt_Indust', 'MaxHt_Mixed', 'LC_Res', 'LC_Comm',
        'LC_Office', 'LC_Indust', 'LC_Mixed', 'SingleFamily_Use', 'MultiFamily_Use'
    ]
    overlay_plan_types_out.to_csv(
        os.path.join(OUTPUT, "flu_qc", 'overlay_plan_types_out_pre_agg_' + str(today) + '.csv'),
        index=False
    )
    agg_col_dict = {col: 'min' for col in min_cols}
    agg_col_dict.update({'overlay_juris_zn': lambda x: '_'.join(x.dropna().astype(str))})
    overlay_plan_types_out = overlay_plan_types_out.groupby('overlay_combo_id').agg(agg_col_dict).reset_index()

    # assign new plan_type_id for each overlay_combo_id
    overlay_plan_types_out['plan_type_id'] = len(f) + np.arange(len(overlay_plan_types_out)) + 1

    # create new juris_zn for overlay combinations
    overlay_plan_types_out['juris_zn'] = overlay_plan_types_out['orig_juris_zn'] + '_' + overlay_plan_types_out['overlay_juris_zn']
    overlay_plan_types_out.drop(columns=['orig_juris_zn', 'overlay_juris_zn'], inplace=True)

    return overlay_plan_types_out


def _update_parcel_xwalk(all_df, parcels_to_update, f):
    """Replace plan_type_id in all_df with the new overlay combo plan_type_id
    for parcels that have an overlay_combo_id."""
    pt_update = parcels_to_update[['parcel_id', 'overlay_combo_id']].merge(
        f[['plan_type_id', 'overlay_combo_id']], on='overlay_combo_id', how='left'
    )[['parcel_id', 'plan_type_id']]

    parcel_to_new_ptid = pt_update.drop_duplicates(subset='parcel_id').set_index('parcel_id')['plan_type_id']
    mask = all_df['parcel_id'].isin(pt_update['parcel_id'])
    all_df.loc[mask, 'plan_type_id'] = all_df.loc[mask, 'parcel_id'].map(parcel_to_new_ptid)

    return all_df


def run_step(context):
    print("Running step: Applying overlays...")
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings.get('unroll_constraints_settings', {})
    global_cfg = p.settings
    ROOT = global_cfg['root_dir']
    OUTPUT = os.path.join(ROOT, "unroll_constraints")
    today = pd.to_datetime("today").date()

    if cfg.get('apply_hb_1110', False):
        f = p.get_table('flu_imputed_hb_1110')
    else:
        f = p.get_table('flu_imputed')

    all_df = p.get_table('parcel_plan_type_xwalk')
    parcels_to_update = _build_parcels_to_update(p, f, all_df)
    overlay_plan_types_out = _build_overlay_plan_types(parcels_to_update, f, OUTPUT, today)

    # add overlay combo zones back to original imputed FLU data
    f = pd.concat([f, overlay_plan_types_out], ignore_index=True)
    # turn -1 values in the use columns back into 0s
    f[USE_COLS] = f[USE_COLS].replace(-1, np.nan).fillna(0)

    all_df = _update_parcel_xwalk(all_df, parcels_to_update, f)

    # save output tables
    if cfg.get('apply_hb_1110', False):
        p.save_table(f, 'flu_imputed_hb_1110_with_overlays')
    else:
        p.save_table(f, 'flu_imputed_with_overlays')

    p.save_table(all_df, 'parcel_plan_type_xwalk_with_overlays')