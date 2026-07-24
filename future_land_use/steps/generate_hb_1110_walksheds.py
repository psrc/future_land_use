import os
import sys

#from future_land_use.util import Pipeline

def generate_walksheds(
    network_dataset_path,
    station_fc_path,
    distance_field_name,
    output_fc_path,
    dissolve_walksheds=False,
    impedance_attribute="Length",
    search_tolerance=5000,
):
    """Generate walkshed polygons (service areas) for a set of station points.

    Parameters
    ----------
    network_dataset_path : str
        Full path to the network dataset.
        Example: <network_gdb_path>/<feature_dataset>/<feature_dataset>_ND
    station_fc_path : str
        Full path to the station points feature class.
        distance_field_name : str or int or float
                Either:
                - A field name on the station points holding per-station walkshed distance,
                    or
                - One constant distance value to use for all stations.
                Distances are in the same units as *impedance_attribute* (typically feet).
    output_fc_path : str
        Full path where the output polygon feature class will be written.
    dissolve_walksheds : bool
        If True, dissolve all walkshed polygons into a single output feature class.
    impedance_attribute : str
        Name of the impedance attribute on the network dataset (default "Length").
    search_tolerance : int or float
        Search tolerance in feet used when locating stations on the network
        (default 5000).
    """

    # ------------------------------------------------------------------
    # 1. Check out the Network Analyst extension
    # ------------------------------------------------------------------
    if arcpy.CheckExtension("network") == "Available":
        arcpy.CheckOutExtension("network")
    else:
        raise RuntimeError("Network Analyst extension is not available.")

    try:
        # ------------------------------------------------------------------
        # 2. Build paths and names
        # ------------------------------------------------------------------
        network_dataset_name = os.path.basename(network_dataset_path)
        if network_dataset_name.endswith("_ND"):
            network_dataset_name = network_dataset_name[:-3]

        scratch_fc = r"memory\stations_snap"
        nd_layer_name = f"{network_dataset_name}_layer"
        junctions_source = f"{network_dataset_name}_ND_Junctions"

        print(f"Network dataset : {network_dataset_path}")
        print(f"Station points  : {station_fc_path}")
        print(f"Output FC       : {output_fc_path}")

        # ------------------------------------------------------------------
        # 3. Copy station points to in-memory scratch (preserves source data)
        # ------------------------------------------------------------------
        print("Copying station points to scratch workspace...")
        if arcpy.Exists(scratch_fc):
            arcpy.management.Delete(scratch_fc)
        arcpy.management.CopyFeatures(station_fc_path, scratch_fc)

        # ------------------------------------------------------------------
        # 4. Pre-snap facilities to junctions using CalculateLocations
        #    search_criteria restricts snapping to junctions only (not edges)
        #    snap_type "SNAP" = Snap to Position Along Network
        # ------------------------------------------------------------------
        print(f"Snapping stations to {junctions_source}...")
        arcpy.na.CalculateLocations(
            in_point_features=scratch_fc,
            in_network_dataset=network_dataset_path,
            search_tolerance=f"{search_tolerance} Feet",
            search_criteria=[[junctions_source, "SHAPE"]],
        )

        # ------------------------------------------------------------------
        # 5. Find the travel mode that uses the requested impedance attribute,
        #    then create the Service Area solver
        # ------------------------------------------------------------------
        print(f"Looking up travel mode with impedance '{impedance_attribute}'...")
        arcpy.nax.MakeNetworkDatasetLayer(network_dataset_path, nd_layer_name)
        travel_modes = arcpy.nax.GetTravelModes(nd_layer_name)

        travel_mode = None
        for name, mode in travel_modes.items():
            if mode.impedance.lower() == impedance_attribute.lower():
                travel_mode = mode
                print(f"  Using travel mode: '{name}'")
                break

        if travel_mode is None:
            available = {n: m.impedance for n, m in travel_modes.items()}
            raise ValueError(
                f"No travel mode found with impedance '{impedance_attribute}'. "
                f"Available travel modes and impedances: {available}"
            )

        print("Creating Service Area solver...")
        sa = arcpy.nax.ServiceArea(nd_layer_name)

        # Analysis settings — travel mode sets the impedance attribute
        sa.travelMode = travel_mode
        sa.distanceUnits = arcpy.nax.DistanceUnits.Feet
        sa.travelDirection = arcpy.nax.TravelDirection.FromFacility

        # Polygon generation: Detailed, Overlapping, Rings
        sa.polygonDetail = arcpy.nax.ServiceAreaPolygonDetail.High
        sa.geometryAtOverlap = arcpy.nax.ServiceAreaOverlapGeometry.Overlap
        sa.geometryAtCutoff = arcpy.nax.ServiceAreaPolygonCutoffGeometry.Rings

        # Use pre-computed locations from CalculateLocations as-is
        sa.allowAutoRelocate = False

        # ------------------------------------------------------------------
        # 6. Build field mappings for facilities
        #    Map distance_field_name → Breaks (per-facility cutoff)
        #    use_location_fields=True includes network location fields so the
        #    pre-calculated CalculateLocations values are picked up by name
        # ------------------------------------------------------------------
        fms = sa.fieldMappings(
            arcpy.nax.ServiceAreaInputDataType.Facilities,
            use_location_fields=True,
        )

        # Resolve Breaks from either a field name or one constant value.
        fields_by_lower = {f.name.lower(): f.name for f in arcpy.ListFields(scratch_fc)}

        matched_field_name = None
        constant_break_value = None

        if isinstance(distance_field_name, (int, float)) and not isinstance(distance_field_name, bool):
            constant_break_value = float(distance_field_name)
        else:
            distance_text = str(distance_field_name).strip()
            matched_field_name = fields_by_lower.get(distance_text.lower())
            if matched_field_name is None:
                try:
                    constant_break_value = float(distance_text)
                except ValueError as ex:
                    available_fields = sorted(fields_by_lower.values())
                    raise ValueError(
                        "distance_field_name must be either an existing field name "
                        "or a numeric distance value. "
                        f"Got: {distance_field_name!r}. "
                        f"Available fields: {available_fields}"
                    ) from ex

        if matched_field_name is not None:
            fms["Breaks"].mappedFieldName = matched_field_name
            print(f"Using per-station distance field: {matched_field_name}")
        else:
            # Breaks is text in the NA input mapping; store the numeric as text.
            fms["Breaks"].defaultValue = str(constant_break_value)
            print(f"Using constant walkshed distance for all stations: {constant_break_value}")

        # ------------------------------------------------------------------
        # 7. Load facilities and solve
        # ------------------------------------------------------------------
        print("Loading facilities...")
        sa.load(arcpy.nax.ServiceAreaInputDataType.Facilities, scratch_fc, fms)

        print("Solving service areas...")
        result = sa.solve()

        # Print any solver messages
        for msg_type, msg_text in result.solverMessages(arcpy.nax.MessageSeverity.All):
            print(f"  [{msg_type}] {msg_text}")

        if not result.solveSucceeded:
            raise RuntimeError("Service Area solve failed. See messages above.")

        # ------------------------------------------------------------------
        # 8. Ensure output GDB exists, then export polygons
        # ------------------------------------------------------------------
        output_gdb_path = os.path.dirname(output_fc_path)
        output_fc_name = os.path.basename(output_fc_path)
        if not arcpy.Exists(output_gdb_path):
            print(f"Creating output geodatabase: {output_gdb_path}")
            gdb_folder = os.path.dirname(output_gdb_path)
            gdb_name = os.path.basename(output_gdb_path)
            arcpy.management.CreateFileGDB(gdb_folder, gdb_name)

        if dissolve_walksheds:
            temp_output_fc_path = os.path.join(output_gdb_path, f"{output_fc_name}__raw")
            if arcpy.Exists(temp_output_fc_path):
                arcpy.management.Delete(temp_output_fc_path)
            if arcpy.Exists(output_fc_path):
                arcpy.management.Delete(output_fc_path)

            print(f"Exporting walkshed polygons to {temp_output_fc_path}...")
            result.export(arcpy.nax.ServiceAreaOutputDataType.Polygons, temp_output_fc_path)

            print(f"Dissolving walkshed polygons to {output_fc_path}...")
            arcpy.management.Dissolve(temp_output_fc_path, output_fc_path)
            arcpy.management.Delete(temp_output_fc_path)
        else:
            if arcpy.Exists(output_fc_path):
                arcpy.management.Delete(output_fc_path)
            print(f"Exporting walkshed polygons to {output_fc_path}...")
            result.export(arcpy.nax.ServiceAreaOutputDataType.Polygons, output_fc_path)

        print("Done.")

    finally:
        # ------------------------------------------------------------------
        # 9. Clean up scratch data and check in extension
        # ------------------------------------------------------------------
        if arcpy.Exists(scratch_fc):
            arcpy.management.Delete(scratch_fc)
        arcpy.CheckInExtension("network")

if __name__ == "__main__":
    import arcpy
    print("Running step: generate_hb_1110_walksheds...")
    #configs_dir = sys.argv[1]  # Expecting a single argument: path to the configs dir
    #p = Pipeline(settings_path=configs_dir)
    #cfg = p.settings['hb1110_settings']
    generate_walksheds(
        network_dataset_path=sys.argv[1],
        station_fc_path=sys.argv[2],
        distance_field_name=sys.argv[3],
        output_fc_path=sys.argv[4],
        dissolve_walksheds=sys.argv[5],
        impedance_attribute=sys.argv[6],
        search_tolerance=sys.argv[7],
    )
