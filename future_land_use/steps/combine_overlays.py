import geopandas as gpd
import numpy as np
import os
import pandas as pd
import yaml

from future_land_use.util.pipeline import Pipeline
from future_land_use.util.clean_unique_ids import clean_id

def load_overlay_layers(gdb_path, layers):
    gdf_out = gpd.GeoDataFrame()
    for layer in layers:
        # grab each layer from the geodatabase and create a juris_zn column if it doesn't exist
        gdf = gpd.read_file(gdb_path, layer=layer['layer'])
        gdf = gdf.to_crs(epsg=2285)
        if 'juris_zn_col' not in layer:
            gdf['juris_zn'] = layer['juris'] + '_' +  gdf[layer['zone_col']]
        elif 'zone_col' not in layer:
            gdf = gdf.rename(columns={layer['juris_zn_col']: 'juris_zn'})
        else:
            raise ValueError(f"Layer {layer['layer']} must have either 'juris_zn_col' or 'zone_col' specified in settings.yaml")
        gdf['juris'] = layer['juris']
        # remove any non-alphanumeric characters from the juris_zn column
        gdf['juris_zn'] = gdf['juris_zn'].apply(clean_id)
        gdf = gdf.dissolve(by='juris_zn', as_index=False)
        gdf_out = pd.concat([gdf_out, gdf[['juris','juris_zn', 'geometry']]], ignore_index=True)
    return gdf_out

def apply_manual_matches(gdf,flu_table,data_dir):
    # bring in manual matches from previous runs if they exist and apply them to the gdf
    manual_match_path = os.path.join(data_dir, 'overlay_manual_match.csv')
    if os.path.exists(manual_match_path):
        print("loading existing manual match file")
        manual_match_df = pd.read_csv(manual_match_path)
        # save backup of the existing manual match file with a timestamp
        backup_path = os.path.join(data_dir, f"overlay_manual_match_backup_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.csv")
        manual_match_df.to_csv(backup_path, index=False)
        # create a mapping of juris_zn to juris_zn_manual_match and apply it to the gdf
        manual_match = manual_match_df.set_index('juris_zn')['juris_zn_manual_match']
        gdf['juris_zn_manual_match'] = gdf['juris_zn'].map(manual_match)
        # if there is a manual match, use it to update the juris_zn column in the gdf
        gdf['juris_zn'] = np.where(gdf['juris_zn_manual_match'].notnull(), gdf['juris_zn_manual_match'], gdf['juris_zn'])

    # merge overlay layers with flu table, flag rows that don't have a match
    gdf = gdf.merge(flu_table, on='juris_zn', how='left')
    gdf['match_flag'] = gdf['juris_zn'].isin(flu_table['juris_zn'])
    gdf_out = gdf.loc[(gdf['match_flag']==False),'juris_zn'].to_frame()

    # if the manual_match_df exists and is not empty, merge the current data to the existing manual 
    # match file, otherwise create the manual_match_df from gdf_out
    if 'manual_match_df' in locals() and not manual_match_df.empty:
        print("merging current data to existing manual match file")
        # remove juris_zn values that were previously unmatched but have now been fixed in the flu table
        fixed_in_table = gdf.loc[(gdf['match_flag']==True) & (gdf['juris_zn_manual_match'].isna()),'juris_zn']
        manual_match_out = manual_match_df.loc[~manual_match_df['juris_zn'].isin(fixed_in_table)].copy()
        manual_match_out = manual_match_out.merge(gdf_out, on='juris_zn', how='left')
    else:
        print("creating manual match file")
        manual_match_out = gdf_out.copy()
        manual_match_out['juris_zn_manual_match'] = np.nan
    
    # save the manual match file for review and editing
    print(f"saving manual match file to {data_dir}/overlay_manual_match.csv. \n"
        "Review this file and fill in the juris_zn_manual_match column with the correct matches, \n"
        "then re-run this script to apply the manual matches to the gdf.")
    manual_match_out.to_csv(f'{data_dir}/overlay_manual_match.csv', index=False)
    return gdf[['juris','juris_zn','match_flag','geometry']]

def run_step(context):
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings.get('overlay_settings', {})
    input_overlay_gdb = cfg.get('overlay_gdb_path', '')

    # load flu table
    flu_table_path = cfg.get('flu_table_path', '')
    flu_juris_zn_col = cfg.get('flu_juris_zn_col', '')
    df = pd.read_excel(flu_table_path)
    df['juris_zn'] = df[flu_juris_zn_col]

    # load overlay gis layers
    gdf = load_overlay_layers(input_overlay_gdb, cfg.get('overlay_layers', []))

    # apply manual matches to the gdf and save the manual match csv for review and editing
    # re-run the scripts after editing the manual match file to apply the manual matches
    gdf = apply_manual_matches(gdf,df,p.get_data_path())
    print(len(gdf[gdf['match_flag']==False]), "rows in the overlay layers that don't have a match in the flu table after applying manual matches")

    # save gdf to pipeline for use in future steps
    p.save_geodataframe(gdf,'overlays_merged')

    # write the combined gdf to gdb for checking
    output_gdb_path = os.path.join(p.get_output_path(), 'overlay_layers_merged.gdb')
    gdf.to_file(output_gdb_path, driver='OpenFileGDB', layer='overlays_merged',promote_to_multi=True)

    # once all unmatched polygons have been corrected, set write_final_overlays_to_input_gdb to True 
    # in settings.yaml to write the final overlay gdf to the original overlay input geodatabase
    if cfg.get('write_final_overlays_to_input_gdb', False):
        today = pd.Timestamp.now().strftime('%Y_%m%d')
        gdf.to_file(input_overlay_gdb, driver='OpenFileGDB', layer=f"{cfg.get('final_overlay_layer_name', 'overlays_combined')}_{today}", promote_to_multi=True)
    return context