import pandas as pd
from future_land_use.util.pipeline import Pipeline


def _build_hb_transit_mask(all_df, pin_name, hb_transit_parcels):
    """Boolean mask for parcels that are HB 1110 transit (tier 1-2),
    residential or mixed-use, and in the transit parcel list."""
    return (
        all_df[pin_name].isin(hb_transit_parcels)
        & all_df['hb_1110_tier'].isin([1, 2])
        & (all_df['Res_Use'].eq(1) | all_df['Mixed_Use'].eq(1))
    )
    return all_df


def _append_hb_transit_suffix(all_df, mask):
    """Append '_hb_transit' to juris_zn for rows matching *mask*."""
    all_df.loc[mask, 'juris_zn'] = all_df.loc[mask, 'juris_zn'].apply(
        lambda x: x + '_hb_transit' if pd.notnull(x) else x
    )
    return all_df


def _remap_hb_transit_plan_types(all_df, f):
    """Map juris_zn to the overridden plan_type_id for HB 1110 transit zones."""
    hb_transit_plan_types = (
        f.loc[f['hb_transit_override'] == 1, ['juris_zn', 'plan_type_id']]
        .set_index('juris_zn')['plan_type_id']
    )
    all_df['plan_type_id'] = all_df['juris_zn'].map(hb_transit_plan_types).fillna(all_df['plan_type_id'])
    return all_df

def run_step(context):
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings.get('unroll_constraints_settings', {})
    pin_name = cfg['parcel_id_col']
    if cfg.get('apply_hb_1110', False):
        print("Running step: Applying HB 1110 to parcels...")
        f = p.get_table('flu_imputed_hb_1110_with_overlays')
        hb_parcels = p.get_table('hb_1110_parcels')
        all_df = (
            p.get_table('parcel_plan_type_xwalk_with_overlays')
            .merge(f[['plan_type_id','hb_1110_tier','Res_Use','Mixed_Use']], on='plan_type_id', how='left')
        )

        #-------- apply HB1110 rules to parcels ---------------------------------
        hb_transit_parcels = hb_parcels.loc[
            (hb_parcels['hb_1110_tier'].isin([1, 2])) & (hb_parcels['hb_1110_transit'] == 1),
            'parcel_id'
        ].tolist()

        mask = _build_hb_transit_mask(all_df, pin_name, hb_transit_parcels)
        all_df = _append_hb_transit_suffix(all_df, mask)
        all_df = _remap_hb_transit_plan_types(all_df, f)
        all_df.drop(columns=['Res_Use','Mixed_Use'], inplace=True)
        p.save_table(all_df, 'parcel_plan_type_xwalk_with_overlays_hb_1110')
    else:
        print("HB 1110 not applied to parcels as per settings.yaml. Skipping step: apply_hb_1110_to_parcels.")