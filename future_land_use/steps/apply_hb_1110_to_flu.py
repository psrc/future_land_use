import pandas as pd
from future_land_use.util import Pipeline


def _apply_min_du_lot(df, tier_vals, res_cond, min_val, du_lot_col, flag_override=None):
    mask = df['hb_1110_tier'].isin(tier_vals) & res_cond
    below_min = mask & (df[du_lot_col] < min_val)
    was_na = mask & (df[du_lot_col].isna())
    df.loc[mask, du_lot_col] = df.loc[mask, du_lot_col].fillna(min_val).apply(lambda x: max(x, min_val))
    if isinstance(flag_override, str) and (below_min | was_na).any():
        df.loc[below_min | was_na, flag_override] = 1
    return df


def _build_hb_cities(hb_parcels):
    """Extract unique city_name → hb_1110_tier mapping, replacing spaces with underscores."""
    hb_cities = hb_parcels.groupby('city_name').agg({'hb_1110_tier': 'first'}).reset_index()
    hb_cities['city_name'] = hb_cities['city_name'].str.replace(' ', '_', regex=True)
    return hb_cities


def _validate_juris_in_hb(f, hb_cities):
    """Raise if any HB city names are missing from the FLU Juris column."""
    flu_juris_set = f['Juris'].unique().tolist()
    missing = hb_cities.loc[~hb_cities['city_name'].isin(flu_juris_set), 'city_name'].tolist()
    if missing:
        raise ValueError(
            'Some Juris values in imputed FLU data do not match city names in HB parcels data. '
            'Check for typos or mismatches.\n'
            f'Mismatched values: {missing}'
        )


def _apply_base_hb_density_rules(f, hb_cities):
    """Merge HB tier info into f and apply minimum density rules for tiers 1-3."""
    du_lot_col = 'FloorMaxDU_lot'
    print("Applying HB 1110 rules...")
    f = f.merge(hb_cities, left_on='Juris', right_on='city_name', how='left')

    print("Applying minimum density rules for HB 1110 tiers...")
    f = _apply_min_du_lot(f, [1], (f['Res_Use'] == 1) | (f['Mixed_Use'] == 1), 4, du_lot_col)
    f = _apply_min_du_lot(f, [2, 3], (f['Res_Use'] == 1) | (f['Mixed_Use'] == 1), 2, du_lot_col)
    return f


def _apply_hb_transit_rules(f, hb_parcels):
    """Create transit-zone plan types with higher minimum densities and override flag."""
    du_lot_col = 'FloorMaxDU_lot'

    # parcel_ids in tier 1 or 2 that also have transit==1
    hb_transit_parcels = hb_parcels.loc[
        (hb_parcels['hb_1110_tier'].isin([1, 2])) & (hb_parcels['hb_1110_transit'] == 1),
        'parcel_id'
    ].tolist()

    # city names that contain at least one transit parcel
    hb_transit_cities = hb_parcels.loc[
        hb_parcels['parcel_id'].isin(hb_transit_parcels), 'city_name'
    ].unique().tolist()

    # filter to residential / mixed-use rows in transit cities
    flu_hb_transit = f.loc[
        f.Juris.isin(hb_transit_cities) & ((f['Res_Use'] == 1) | (f['Mixed_Use'] == 1))
    ]

    # keep only rows that need a higher minimum
    flu_hb_transit = flu_hb_transit[
        ((flu_hb_transit['hb_1110_tier'] == 1) & (flu_hb_transit[du_lot_col] < 6)) |
        ((flu_hb_transit['hb_1110_tier'].isin([2, 3])) & (flu_hb_transit[du_lot_col] < 4))
    ]
    flu_hb_transit['juris_zn'] = flu_hb_transit['juris_zn'] + '_hb_transit'

    print("Creating minimum density rules for HB 1110 transit zones...")
    flu_hb_transit = _apply_min_du_lot(
        flu_hb_transit, [1],
        (flu_hb_transit['Res_Use'] == 1) | (flu_hb_transit['Mixed_Use'] == 1),
        6, du_lot_col, flag_override='hb_transit_override',
    )
    flu_hb_transit = _apply_min_du_lot(
        flu_hb_transit, [2, 3],
        (flu_hb_transit['Res_Use'] == 1) | (flu_hb_transit['Mixed_Use'] == 1),
        4, du_lot_col, flag_override='hb_transit_override',
    )

    # add transit zones to the main FLU data
    f = pd.concat([f, flu_hb_transit], ignore_index=True)
    return f


def run_step(context):
    print("Running step: apply_hb_1110_to_flu...")
    p = Pipeline(settings_path=context['configs_dir'])

    f = p.get_table('flu_imputed')

    print("Reading in HB1110 parcels...")
    hb_parcels = p.get_table('hb_1110_parcels')

    hb_cities = _build_hb_cities(hb_parcels)
    _validate_juris_in_hb(f, hb_cities)
    f = _apply_base_hb_density_rules(f, hb_cities)
    f = _apply_hb_transit_rules(f, hb_parcels)

    p.save_table(f, 'flu_imputed_hb_1110')
    return context