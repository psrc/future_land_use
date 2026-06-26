import geopandas as gpd
import numpy as np
import os
import pandas as pd
from future_land_use.util import Pipeline

def _load_flu_shp(p, path, juris_zn_id_col):
    """Read in the FLU GIS layer shapefile, standardise CRS, and save to pipeline."""
    print(f"Reading in FLU shapefile {path}...")
    flu_shp = (
        gpd.read_file(path)
        .rename(columns={juris_zn_id_col: 'juris_zn'})
    )[['juris_zn', 'geometry']]
    flu_shp = flu_shp.to_crs(epsg=2285)
    p.save_geodataframe(flu_shp, 'flu_shp')


def _load_flu_imputed(p, path, juris_zn_id_col):
    """Read in the FLU imputed CSV, rename ID column, drop cruft, and save to pipeline."""
    print(f"Reading in FLU imputed data {path}...")
    f = (
        pd.read_csv(path)
        .rename(columns={juris_zn_id_col: 'juris_zn'})
    )
    # clean up f; remove extra/unnecessary fields before join
    drop_cols = [col for col in f.columns if col in ['Key', 'Zone', 'Definition'] or col.endswith('src')]
    f = f.drop(columns=drop_cols)
    p.save_table(f, 'flu_imputed')


def _load_parcel_land_use(p, cache_dir, pin_name):
    """Read in parcel land use type, TOD, and gross sqft from urbansim cache."""
    print(f"Reading in parcel land use type from urbansim cache {cache_dir}...")
    prcls_pin = np.fromfile(os.path.join(cache_dir, 'parcel_id.li8'), np.int64)  # BY 2023
    prcls_lu = np.fromfile(os.path.join(cache_dir, 'land_use_type_id.li8'), np.int64)  # BY 2023
    prcls_tod = np.fromfile(os.path.join(cache_dir, 'tod_id.li4'), np.int32)
    prcls_sqft = np.fromfile(os.path.join(cache_dir, 'gross_sqft.lf8'), np.float64)
    lu_type = pd.DataFrame(
        {pin_name: prcls_pin, 'lu_type': prcls_lu, 'tod_id': prcls_tod, 'gross_sqft': prcls_sqft},
        index=prcls_pin,
    )
    p.save_table(lu_type, 'parcels_land_use_type')


def run_step(context):
    print("Running step: load_data...")
    # -- load settings
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings.get('unroll_constraints_settings', {})
    ROOT = cfg['root_dir']
    
    # flu gis layer
    FLU_SHP_PATH = os.path.join(ROOT, cfg['flu_shp'])
    juris_zn_shp_id = cfg['juris_zn_shp_id'] # unique id column

    # imputed flu
    FLU_IMP = cfg['flu_imputed'] 
    FLU_IMP_PATH = os.path.join(ROOT, FLU_IMP)
    juris_zn_imputed_id = cfg['juris_zn_imputed_id'] # unique id column
    
    # urbansim baseyear cache
    CACHE = cfg['base_year_cache']  # BY 2023
    pin_name = cfg['parcel_id_col'] # unique id column in parcels file

    # -- load data
    _load_flu_shp(p, FLU_SHP_PATH, juris_zn_shp_id)
    _load_flu_imputed(p, FLU_IMP_PATH, juris_zn_imputed_id)
    _load_parcel_land_use(p, CACHE, pin_name)
    return context