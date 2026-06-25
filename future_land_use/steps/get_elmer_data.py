from psrcelmerpy import ElmerGeoConn
from future_land_use.util import Pipeline

def run_step(context):
    print("Running step: get_elmer_data...")
    p = Pipeline(settings_path=context['configs_dir'])
    layers = p.settings['elmer_geo_layers']
    for layer in layers:
        gdf = ElmerGeoConn().read_geolayer(layers[layer])
        p.save_geodataframe(gdf, layer)
    return context