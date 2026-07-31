import os
import subprocess
from future_land_use.steps import generate_hb_1110_walksheds
from future_land_use.util import Pipeline

def run_step(context):
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings['hb1110_settings']

    network_dataset_path=p.get_onedrive_path(cfg['network_dataset_path'])
    station_fc_path1=p.get_onedrive_path(cfg['transit_gdb_path'], cfg['station_fc_name'])
    station_fc_path2= r"C:\Users\scoe\PSRC\GIS - Sharing\Projects\FLU\transit_hb_1110.gdb\hb_1110_city_stops"
    distance_field_name=cfg['distance_field_name']
    output_fc_path=p.get_onedrive_path(cfg['transit_gdb_path'], cfg['transit_walksheds_layer'])
    dissolve_walksheds=cfg['dissolve_walksheds']
    impedance_attribute=cfg['impedance_attribute']
    search_tolerance=cfg['search_tolerance']


    target_python = cfg["arcpy_python_path"]
    subprocess.run(
        [
            target_python,
            "-m",
            "future_land_use.steps.generate_hb_1110_walksheds",
            str(network_dataset_path),
            str(station_fc_path1),
            str(distance_field_name),
            str(output_fc_path),
            str(dissolve_walksheds),
            str(impedance_attribute),
            str(search_tolerance),
        ],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=True,
    )