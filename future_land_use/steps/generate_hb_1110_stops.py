import transit_service_analyst as tsa
from pathlib import Path
import pandas as pd
import geopandas as gpd
from dataclasses import dataclass
import configuration
import yaml
import psrcelmerpy
from future_land_use.util.pipeline import Pipeline


@dataclass
class transit_data_frames:
    """A class to hold transit data frames."""

    year: str
    routes: gpd.GeoDataFrame
    route_stops: gpd.GeoDataFrame
    stops: gpd.GeoDataFrame


def service_by_route_attribute(
    transit_dfs: transit_data_frames, field, values: list
) -> gpd.GeoDataFrame:
    """
    Get the service by route type from the transit data frames.
    """
    route_list = []
    route_stop_list = []
    stop_list = []

    routes = transit_dfs.routes[transit_dfs.routes[field].isin(values)]
    route_stops = transit_dfs.route_stops[
        transit_dfs.route_stops["rep_trip_id"].isin(routes["rep_trip_id"].to_list())
    ]
    stops = transit_dfs.stops[
        transit_dfs.stops["stop_id"].isin(route_stops["stop_id"].to_list())
    ]
    # stops = add_columns_to_stops(stops, route_type, transit_dfs.year)
    route_list.append(routes)
    route_stop_list.append(route_stops)
    stop_list.append(stops)

    routes = gpd.GeoDataFrame(pd.concat(route_list))
    routes.crs = 4326
    routes = routes.to_crs(2285)

    route_stops = gpd.GeoDataFrame(pd.concat(route_stop_list))
    route_stops.crs = 4326
    route_stops = route_stops.to_crs(2285)

    stops = pd.concat(stop_list, ignore_index=True)
    stops.crs = 4326
    stops = stops.to_crs(2285)

    return transit_data_frames(
        year=transit_dfs.year, routes=routes, route_stops=route_stops, stops=stops
    )


def combine_transit_services(
    current_brt_routes: transit_data_frames,
    current_transit_routes: transit_data_frames,
    future_brt_routes: transit_data_frames,
    future_transit_routes: transit_data_frames,
) -> transit_data_frames:
    """
    Combine current/future BRT and transit datasets into a single container.
    """
    route_frames = [
        current_brt_routes.routes,
        current_transit_routes.routes,
        future_brt_routes.routes,
        future_transit_routes.routes,
    ]
    route_stop_frames = [
        current_brt_routes.route_stops,
        current_transit_routes.route_stops,
        future_brt_routes.route_stops,
        future_transit_routes.route_stops,
    ]
    stop_frames = [
        current_brt_routes.stops,
        current_transit_routes.stops,
        future_brt_routes.stops,
        future_transit_routes.stops,
    ]

    routes = gpd.GeoDataFrame(pd.concat(route_frames, ignore_index=True))
    if "rep_trip_id" in routes.columns:
        routes = routes.drop_duplicates(subset=["rep_trip_id"])
    elif "route_id" in routes.columns:
        routes = routes.drop_duplicates(subset=["route_id"])

    route_stops = gpd.GeoDataFrame(pd.concat(route_stop_frames, ignore_index=True))
    if {"rep_trip_id", "stop_id"}.issubset(route_stops.columns):
        route_stops = route_stops.drop_duplicates(subset=["rep_trip_id", "stop_id"])

    stops = gpd.GeoDataFrame(pd.concat(stop_frames, ignore_index=True))
    if "stop_id" in stops.columns:
        stops = stops.drop_duplicates(subset=["stop_id"])

    return transit_data_frames(
        year=f"{current_brt_routes.year}_{future_transit_routes.year}",
        routes=routes,
        route_stops=route_stops,
        stops=stops,
    )


def get_brt_service(
    transit_dfs: transit_data_frames, brt_routes: dict
) -> gpd.GeoDataFrame:
    """
    Get get current BRT routes from the transit data frames.
    """
    return service_by_route_attribute(
        transit_dfs,
        "route_id",
        list(brt_routes.values()),
    )


def add_columns_to_stops(
    stops: gpd.GeoDataFrame, stop_type: int, year: int
) -> pd.DataFrame:
    """
    Add a column to the stops GeoDataFrame with a constant value.
    """
    stops["stop_type"] = stop_type
    # stops['buffer_size'] = buffer_size
    stops["year"] = year
    return stops


def add_route_type_to_tsa_stops(tsa_instance) -> pd.DataFrame:
    """
    Add route_type to tsa_instance.stops by joining GTFS tables.

    Join path:
    - stop_times.stop_id -> stops.stop_id
    - stop_times.route_id -> routes.route_id (if route_id exists on stop_times)
    - otherwise stop_times.trip_id -> trips.trip_id -> trips.route_id -> routes.route_id
    """
    required_tsa_tables = ["stops", "stop_times", "routes"]
    for table_name in required_tsa_tables:
        if not hasattr(tsa_instance, table_name):
            raise ValueError(f"tsa_instance is missing required table: {table_name}")

    stops_df = tsa_instance.stops.copy()
    stop_times_df = tsa_instance.stop_times.copy()
    routes_df = tsa_instance.routes.copy()

    if "stop_id" not in stops_df.columns:
        raise ValueError("stops table is missing stop_id")
    if "stop_id" not in stop_times_df.columns:
        raise ValueError("stop_times table is missing stop_id")
    if "route_type" not in routes_df.columns:
        raise ValueError("routes table is missing route_type")

    if "route_id" in stop_times_df.columns:
        stop_route_types = stop_times_df[["stop_id", "route_id"]].merge(
            routes_df[["route_id", "route_type"]], on="route_id", how="left"
        )
    else:
        if "trip_id" not in stop_times_df.columns:
            raise ValueError("stop_times table is missing trip_id")
        if not hasattr(tsa_instance, "trips"):
            raise ValueError("tsa_instance is missing trips table needed for route mapping")

        trips_df = tsa_instance.trips.copy()
        if not {"trip_id", "route_id"}.issubset(trips_df.columns):
            raise ValueError("trips table is missing trip_id or route_id")

        stop_route_types = (
            stop_times_df[["stop_id", "trip_id"]]
            .merge(trips_df[["trip_id", "route_id"]], on="trip_id", how="left")
            .merge(routes_df[["route_id", "route_type"]], on="route_id", how="left")
        )

    stop_route_types = (
        stop_route_types.dropna(subset=["route_type"])
        .drop_duplicates(subset=["stop_id", "route_type"])
        .groupby("stop_id")["route_type"]
        .agg(lambda values: ",".join(sorted(set(values.astype(str)))))
        .reset_index()
    )

    enriched_stops = stops_df.merge(stop_route_types, on="stop_id", how="left")

    missing_route_type_stops = (
        enriched_stops.loc[
            enriched_stops["route_type"].isna(),
            "stop_id",
        ]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )
    if missing_route_type_stops:
        raise ValueError(
            "Missing route_type for stops after GTFS merge. "
            f"Count: {len(missing_route_type_stops)}. "
            f"Examples: {missing_route_type_stops[:10]}"
        )

    enriched_stops["route_type"] = enriched_stops["route_type"].astype(str)

    tsa_instance.stops = enriched_stops
    return enriched_stops


def get_transit_gdfs(gtfs_dir: Path, date: str) -> tuple:
    """
    Get the transit GeoDataFrames for a given service definition.
    """
    tsa_instance = tsa.load_gtfs(gtfs_dir, date)
    # get the routes
    dfs = transit_data_frames(
        year=date[:4],
        routes=tsa_instance.get_lines_gdf(),
        route_stops=tsa_instance.get_line_stops_gdf(),
        stops=gpd.GeoDataFrame(
            tsa_instance.stops,
            geometry=gpd.points_from_xy(
                tsa_instance.stops["stop_lon"], tsa_instance.stops["stop_lat"]
            ),
        ),
    )

    return dfs


def get_stops_intersecting_cities(
    stops_gdf: gpd.GeoDataFrame, cities_gdf: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """
    Return only stops that spatially intersect city polygons.
    """
    if stops_gdf.empty or cities_gdf.empty:
        return gpd.GeoDataFrame(stops_gdf.iloc[0:0].copy(), crs=stops_gdf.crs)
    if stops_gdf.crs is None:
        raise ValueError("stops_gdf must have a CRS set before spatial join")
    if cities_gdf.crs is None:
        raise ValueError("cities_gdf must have a CRS set before spatial join")

    if stops_gdf.crs != cities_gdf.crs:
        cities_gdf = cities_gdf.to_crs(stops_gdf.crs)

    city_geometry = cities_gdf[["geometry"]].copy()
    city_stops = gpd.sjoin(
        stops_gdf,
        city_geometry,
        how="inner",
        predicate="intersects",
    )
    city_stops = city_stops.drop(columns=["index_right"], errors="ignore")

    if "stop_id" in city_stops.columns:
        city_stops = city_stops.drop_duplicates(subset=["stop_id"])
    else:
        city_stops = city_stops[~city_stops.index.duplicated(keep="first")]

    return gpd.GeoDataFrame(city_stops, geometry="geometry", crs=stops_gdf.crs)


file = Path().joinpath(configuration.args.configs_dir, "config.yaml")

config = yaml.safe_load(open(file))


def run_step(context):
    p = Pipeline(settings_path=context['configs_dir'])
    cfg = p.settings.get('overlay_settings', {})

    current_transit_gdfs = get_transit_gdfs(
    Path(cfg["gtfs_dir_current"]), cfg["gtfs_date_current"]
)

    future_transit_gdfs = get_transit_gdfs(
        Path(cfg["gtfs_dir_future"]), cfg["gtfs_date_future"]
    )


    current_brt_gdfs = service_by_route_attribute(
        current_transit_gdfs, "route_id", list(cfg["current_brt_routes"].values())
    )
    assert len(current_brt_gdfs.routes.route_id.unique()) == len(
        cfg["current_brt_routes"].values()
    ), "Not all BRT routes were found in the GTFS data."
    future_brt_gdfs = service_by_route_attribute(
        future_transit_gdfs, "route_id", list(cfg["future_brt_routes"].values())
    )
    assert len(future_brt_gdfs.routes.route_id.unique()) == len(
        cfg["future_brt_routes"].values()
    ), "Not all future BRT routes were found in the GTFS data."

    current_transit_gdfs = service_by_route_attribute(
        current_transit_gdfs, "route_type", [0,2,5]
    )

    future_transit_gdfs = service_by_route_attribute(future_transit_gdfs, "route_type", [1])

    dfs = combine_transit_services(
        current_brt_gdfs,
        current_transit_gdfs,
        future_brt_gdfs,
        future_transit_gdfs,
    )

    eg_conn = psrcelmerpy.ElmerGeoConn()
    cities = eg_conn.read_geolayer('cities')
    cities = cities[cities['hb_1110_tier'] > 0]

    city_stops_gdf = get_stops_intersecting_cities(dfs.stops, cities)

    dfs.stops.to_file(
        Path(cfg["output_gdb"]), driver="OpenFileGDB", layer="hb_1110_stops"
    )
    city_stops_gdf.to_file(
        Path(cfg["output_gdb"]), driver="OpenFileGDB", layer="hb_1110_city_stops"
    )
    dfs.route_stops.to_file(
        Path(cfg["output_gdb"]), driver="OpenFileGDB", layer="hb_1110_route_stops"
    )
    dfs.routes.to_file(Path(cfg["output_gdb"]), driver="OpenFileGDB", layer="hb_1110_routes")


    print("done")
