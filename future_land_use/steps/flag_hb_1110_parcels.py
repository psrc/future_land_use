import geopandas as gpd
from psrcelmerpy import ElmerGeoConn
from future_land_use.util import Pipeline

def get_elmer_geo_layer(layer_name):
    elmer_conn = ElmerGeoConn()
    return elmer_conn.read_geolayer(layer_name)

def load_transit_walkshed(gdb_path, layer_name):
    return gpd.read_file(gdb_path, layer=layer_name).to_crs("EPSG:2285")

def flag_transit_parcels(parcels, cities, hct_walkshed):
    # filter hb1110 cities and spatial join to parcels
    cities = cities.loc[cities['hb_1110_tier'] > 0].copy()
    parcels_cities = parcels[['parcel_id','geometry']].sjoin(
        cities[['city_name','hb_1110_tier','geometry']]
    ).drop(columns=['index_right'])
    # spatial join parcels to transit walkshed and flag them
    parcels_cities_transit = parcels_cities.sjoin(
        hct_walkshed[['geometry']], how='left'
    )
    parcels_cities_transit['hb_1110_transit'] = 0
    parcels_cities_transit.loc[
        parcels_cities_transit['index_right'].notnull(), 'hb_1110_transit'
    ] = 1
    # remove transit flag for tier 3 cities, only tier 1 and 2 have transit requirements
    parcels_cities_transit.loc[parcels_cities_transit['hb_1110_tier']==3, 'hb_1110_transit'] = 0
    
    return parcels_cities_transit.drop(columns=['index_right'])

def export_dissolved_parcels(parcel_polygons, parcels_flags, output_dir, output_layer):
    joined = parcel_polygons[['parcel_id','geometry']].merge(
        parcels_flags[['parcel_id','city_name','hb_1110_tier','hb_1110_transit']], 
        on='parcel_id'
    )
    dissolved = joined.dissolve(
        by=['city_name','hb_1110_tier','hb_1110_transit'], 
        as_index=False
    ).drop(columns='parcel_id')
    dissolved.to_file(
        f'{output_dir}/{output_layer}.gdb',
        layer=output_layer,
        driver='OpenFileGDB',
        promote_to_multi=True
    )

def run_step(context):
    print("Running step: flag_hb_1110_parcels...")
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings['hb1110_settings']
    data_dir = p.get_data_path()
    output_dir = p.get_output_path()
    
    hct_walkshed = load_transit_walkshed(p.get_onedrive_path(cfg['transit_gdb_path']), cfg['transit_walksheds_layer'])
    cities = get_elmer_geo_layer('CITIES')
    parcels = get_elmer_geo_layer('PARCELS_URBANSIM_2023_PTS')
    hct_walkshed = load_transit_walkshed(cfg['transit_gdb_path'], cfg['transit_walksheds_layer'])
    cities = p.get_geodataframe('cities')
    parcels = p.get_geodataframe('parcels_pts')
    
    parcels_flags = flag_transit_parcels(parcels, cities, hct_walkshed)
    p.save_table(parcels_flags.drop(columns=['geometry']),'hb_1110_parcels',)
    
    if cfg.get('output_cities_walkshed', False):
        # parcel polygons are not saved in the pipeline due to size and are only needed
        # for doing a visual check of the parcel-to-walkshed spatial join.
        output_layer = cfg['output_cities_walkshed_name']
        parcel_polygons = get_elmer_geo_layer('PARCELS_URBANSIM_2023')
        export_dissolved_parcels(parcel_polygons, parcels_flags, output_dir, output_layer)
    return context