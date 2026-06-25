import geopandas as gpd
from future_land_use.util import Pipeline

def run_step(context):
    print("Running step: flag_overlay_parcels...")
    p = Pipeline(settings_path=context['configs_dir'])
    overlays = p.get_geodataframe('overlays_merged')
    parcels = p.get_geodataframe('parcels_pts')
    gdf = parcels[['parcel_id','geometry']].sjoin(
        overlays[['juris_zn','geometry']]
    )
    p.save_table(gdf[['parcel_id','juris_zn']], 'overlay_parcels')
    return context