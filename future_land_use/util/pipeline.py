import pandas as pd
import yaml
from pathlib import Path
import os
import geopandas as gpd
from shapely.wkt import loads


class Pipeline:
    def __init__(self, settings_path='configs'):
        """
        Initialize Pipeline with settings loaded from a YAML file.
        """
        self.settings_path = Path(settings_path).resolve()
        self.base_dir = self.settings_path.parent

        with open(self.settings_path / 'settings.yaml', 'r') as file:
            self.settings = yaml.safe_load(file)

        # create data and output directories if they don't exist
        create_directory(path=self.get_data_path())
        create_directory(path=self.get_output_path())

    def get_settings_path(self):
        # Returns the path to the settings directory
        return str(self.settings_path)

    def _resolve_workspace_path(self, configured_path, default_name):
        path = Path(configured_path or default_name)
        if not path.is_absolute():
            path = self.base_dir / path
        return path

    def get_data_path(self, *path_parts):
        return self._resolve_workspace_path(self.settings.get('data_dir'), 'data').joinpath(*path_parts)

    def get_output_path(self, *path_parts):
        return self._resolve_workspace_path(self.settings.get('output_dir'), 'output').joinpath(*path_parts)

    def get_hdf5_path(self):
        return self.get_data_path('pipeline.h5')
    
    def get_output_table_list(self):
        # Returns a list of output table names from settings.yaml
        return self.settings.get('output_table_list', [])

    def get_table(self, table_name):
        with pd.HDFStore(self.get_hdf5_path(), mode='r') as h5store:
            return h5store.get(table_name)

    def save_table(self, table_name, df):
        print(f"Saving table {table_name} to HDF5 store...")
        with pd.HDFStore(self.get_hdf5_path(), mode='a') as h5store:
            h5store.put(table_name, df, format='table')

    def save_geodataframe(self, name, gdf):
        gdf['geometry_wkt'] = gdf.geometry.to_wkt()
        gdf_to_save = gdf.drop(columns=['geometry'])
        self.save_table(name, gdf_to_save)

    def get_geodataframe(self, name,crs='epsg:2285'):
        df = self.get_table(name)
        df['geometry'] = df['geometry_wkt'].apply(loads)
        gdf = gpd.GeoDataFrame(df, geometry='geometry', crs=crs)
        gdf = gdf.drop(columns=['geometry_wkt'])
        return gdf


def create_directory(path_parts: list=None, path: str=None) -> Path:
    """Create a directory if it doesn't exist."""
    if path_parts:
        path = Path(os.path.join(*path_parts))
    else:
        path_parts = path

    if not os.path.exists(path):
        os.makedirs(path)
        print(f"Directory {path} created.")