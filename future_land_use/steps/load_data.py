import glob
import geopandas as gpd
import numpy as np
import os
import pandas as pd
import re
from datetime import datetime
from future_land_use.util import Pipeline

def _load_flu_shp(p, path, rename_dict=None):
    """Read in the FLU GIS layer shapefile, standardise CRS, and save to pipeline."""
    print(f"Reading in FLU shapefile {path}...")
    flu_shp = gpd.read_file(path)
    if rename_dict:
        flu_shp = flu_shp.rename(columns=rename_dict)
    flu_shp = flu_shp[['juris_zn', 'geometry']]
    flu_shp = flu_shp.to_crs(epsg=2285)
    p.save_geodataframe(flu_shp, 'flu_shp')

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


def _load_flu_table(p, path, sheet, rename_dict=None):
    """Read in the new-vintage FLU Excel table and save to pipeline."""
    print(f"Reading in FLU table {path}...")
    flu_table = pd.read_excel(path, sheet_name=sheet)
    # Convert object columns to string to avoid mixed-type Arrow errors (e.g. Zone column
    # with both int and string values)
    obj_cols = flu_table.select_dtypes(include='object').columns
    flu_table[obj_cols] = flu_table[obj_cols].astype(str)
    if rename_dict:
        flu_table = flu_table.rename(columns=rename_dict)
    p.save_table(flu_table, 'flu_table')


def _load_old_flu_shp(p, path, rename_dict=None):
    """Read in the old-vintage FLU shapefile, standardise CRS, and save to pipeline."""
    print(f"Reading in old FLU shapefile {path}...")
    old_flu_shp = gpd.read_file(path)
    if rename_dict:
        old_flu_shp = old_flu_shp.rename(columns=rename_dict)
    p.save_geodataframe(old_flu_shp, 'old_flu_shp')


def _load_old_flu_crosswalk(p, path, sheet, rename_dict=None):
    """Read in the old FLU crosswalk Excel table and save to pipeline."""
    print(f"Reading in old FLU crosswalk {path}...")
    old_xwalk = (
        pd.read_excel(path, sheet_name=sheet)
        [["FLUadj_Key", "FLUadj_Definition"]]
        .dropna()
    )
    if rename_dict:
        old_xwalk = old_xwalk.rename(columns=rename_dict)
    p.save_table(old_xwalk, 'old_flu_crosswalk')


def run_step(context):
    print("Running step: load_data...")
    # -- load settings
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings.get('unroll_constraints_settings', {})
    global_cfg = p.settings
    old_flu_cfg = p.settings.get('old_flu_crosswalk_settings', {})
    ROOT = global_cfg['root_dir']
    
    FLU_TABLE_PATH = global_cfg['flu_table_path']
    FLU_TABLE_SHEET = global_cfg['flu_table_sheet']

    # flu gis layer
    FLU_SHP_PATH = os.path.join(ROOT, global_cfg['flu_shp'])
    flu_shp_rename_cols = global_cfg.get('flu_shp_rename_cols', {})

    # urbansim baseyear cache
    CACHE = cfg['base_year_cache']  # BY 2023
    pin_name = cfg['parcel_id_col'] # unique id column in parcels file

    # old flu crosswalk inputs
    old_flu_shp_rename = old_flu_cfg.get('old_flu_shp_rename_cols', {})
    old_flu_xwalk_rename = old_flu_cfg.get('old_crosswalk_rename_cols', {})

    # -- load data
    _load_flu_shp(p, FLU_SHP_PATH, rename_dict=flu_shp_rename_cols)
    _load_parcel_land_use(p, CACHE, pin_name)
    _load_flu_table(p, FLU_TABLE_PATH, FLU_TABLE_SHEET, rename_dict=global_cfg.get('flu_table_rename_cols', {}))
    _load_old_flu_shp(p, old_flu_cfg['old_flu_shp'],rename_dict=old_flu_shp_rename)
    _load_old_flu_crosswalk(p, old_flu_cfg['old_crosswalk'], old_flu_cfg['old_crosswalk_sheet'],rename_dict=old_flu_xwalk_rename)
    return context