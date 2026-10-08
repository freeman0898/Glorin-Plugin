#- - coding: utf-8 - -
""" 
GLORIN plugin — segmented reach analysis.
Selects a single river by its ID, splits it into segments of 
equal length or equal count, then computes sinuosity and 
environmental indices (NDVI, NDWI, MNDWI) for each segment 
independently. Outputs one feature per segment with its geometry 
and statistics as attributes.
Intended for the GLORIN project — quantifying how environmental 
factors vary along the length of a river. 
"""

import json    
import logging 
from typing import Any, Optional

import ee

from qgis.core import ( 
    QgsProcessingAlgorithm, 
    QgsProcessingException, 
    QgsProcessingParameterFeatureSource, 
    QgsProcessingParameterField, 
    QgsProcessingParameterString, 
    QgsProcessingParameterCrs, 
    QgsProcessingParameterNumber, 
    QgsProcessingParameterEnum, 
    QgsProcessingParameterDateTime, 
    QgsProcessingParameterFeatureSink, 
    QgsDistanceArea, 
    QgsCoordinateTransform, 
    QgsProject, 
    QgsFields, 
    QgsField, 
    QgsFeature, 
    QgsGeometry, 
    QgsPointXY,
    QgsWkbTypes, 
    QgsProcessing, 
    QgsCoordinateReferenceSystem, 
    QgsVectorLayer, 
    QgsFeatureSink, 
    )

from PyQt5.QtCore import QVariant 
from qgis import processing

from .manual_segment_analysis import ( 
    SENSOR_CONFIG, 
    SENSOR_NAMES, 
    INDEX_FUNCTIONS, 
    INDEX_NAMES, 
    STAT_OPTIONS, 
    STAT_NAMES, 
    build_reducer, 
    scale_landsat_c2, )

logger = logging.getLogger(__name__)


STAT_EE_SUFFIX = { 
    "Mean": "mean", 
    "Median": "median", 
    "Variance": "variance", 
    "Skewness": "skew", 
    "Min": "min", 
    "Max": "max", 
    "25th Percentile": "p25", 
    "75th Percentile": "p75", }

def qgs_geometry_to_ee_geometry(geom, source_crs, context): 
    """Convert a QgsGeometry to an ee.Geometry in EPSG:4326.

    Earth Engine requires coordinates in WGS84 geographic. We transform
    the geometry, serialise it to GeoJSON, and pass that to ee.Geometry.
    Working on a copy ensures the original is not mutated.
    """
    target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(
        source_crs, target_crs, context.transformContext())

    geom = QgsGeometry(geom)
    geom.transform(transform)

    geojson_dict = json.loads(geom.asJson())
    if geojson_dict is None or "coordinates" not in geojson_dict:
        raise ValueError("Geometry produced invalid GeoJSON.")

    return ee.Geometry(geojson_dict)
    
def ensure_northern_start(geom: QgsGeometry) -> QgsGeometry:
    """Flip the line so its first vertex is the more NORTHERN endpoint.

    Larger Y = further north in the projected working CRS (must NOT be
    called on geographic coordinates, where Y is degrees latitude — still
    "north" but the comparison is equally valid there; the real requirement
    is just that the geometry is already in the CRS we compare against).

    Handles single-part and multipart lines: the comparison uses the first
    vertex of the FIRST part vs the last vertex of the LAST part. If the
    last end is more northern, every part is reversed AND the part order is
    reversed, so the geometry reads north-to-south overall.

    Returns a NEW geometry when flipping (the input is never mutated);
    returns the input unchanged when it already starts in the north, or
    when both endpoints share the same Y (east-west tie — original
    digitizing direction is kept, which keeps behaviour deterministic).
    """
    lines = geom.asMultiPolyline()
    if not lines:                      # single-part line
        lines = [geom.asPolyline()]

    start_pt = lines[0][0]              # first vertex overall
    end_pt = lines[-1][-1]              # last vertex overall

    if end_pt.y() <= start_pt.y():
        return geom                     # already northern-start (or tie)

    # Reverse each part, and reverse the order of the parts.
    parts = [QgsGeometry.fromPolylineXY(list(reversed(part)))
             for part in reversed(lines)]
    if len(parts) == 1:
        return parts[0]
    return QgsGeometry.collectGeometry(parts)   
    
def split_river_by_length(river_layer, segment_length_m, context, feedback): 
    """Split a line layer into segments of approximately equal length.

    Delegates to QGIS's native splitlinesbylength algorithm. Each output
    feature is a LineString of roughly segment_length_m metres. The last
    segment may be shorter if the total length isn't evenly divisible.
    """
    result = processing.run(
        "native:splitlinesbylength",
        {
            "INPUT": river_layer,
            "LENGTH": segment_length_m,
            "OUTPUT": "memory:",
        },
        context=context,
        is_child_algorithm=True,
    )
    return context.getMapLayer(result["OUTPUT"])

class SegmentedReachAnalysisAlgorithm(QgsProcessingAlgorithm): 
    """ Select a single river by ID, split it into segments, 
    and run sinuosity + environmental index calculations on each segment. """

    # --- Parameter names ---
    INPUT = "INPUT"
    CRS = "CRS"
    ID_FIELD = "ID_FIELD"
    RIVER_ID = "RIVER_ID"
    SEGMENT_MODE = "SEGMENT_MODE"
    SEGMENT_LENGTH = "SEGMENT_LENGTH"
    SEGMENT_COUNT = "SEGMENT_COUNT"
    BUFFER_METERS = "BUFFER_METERS"
    SENSOR = "SENSOR"
    START_DATE = "START_DATE"
    END_DATE = "END_DATE"
    INDEX = "INDEX"
    MAX_CLOUD = "MAX_CLOUD"
    STATS = "STATS"
    OUTPUT = "OUTPUT"

    # --- QGIS-required metadata methods ---

    def name(self):
        return "segmented_reach_analysis"

    def displayName(self):
        return "Segmented Reach Analysis (Sinuosity + Indices by Segment)"

    def group(self):
        return "GLORIN"

    def groupId(self):
        return "glorin"

    def shortHelpString(self):
        return (
            "<b>Segmented Reach Analysis</b><br>"
            "Selects a single river from a line layer by its ID, splits it "
            "into segments (by length or by count), and computes sinuosity "
            "plus environmental indices (NDVI / NDWI / MNDWI) for each "
            "segment independently. Output is one feature per segment with "
            "its geometry and statistics as attributes.<br><br>"
            "<b>Split by:</b> 'Length (km)' splits the river every N km; "
            "'Number of segments' divides the river into N equal-length "
            "segments. Segments start counting from the northern vertex"
            "of the river and move towards the southern vertex <br><br>"
            "Intended for the GLORIN project."
        )

    def createInstance(self):
        return SegmentedReachAnalysisAlgorithm()

    def initAlgorithm(self, config):

        # --- River selection ---
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT,
            "River line layer",
            [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS,
            "Working CRS (UTM)"))
        self.addParameter(QgsProcessingParameterField(
            self.ID_FIELD,
            "River ID field",
            parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterString(
            self.RIVER_ID,
            "River ID value"))

        # --- Segment parameters ---
        self.addParameter(QgsProcessingParameterEnum(
            self.SEGMENT_MODE,
            "Split by",
            options=["Length (km)", "Number of segments"],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.SEGMENT_LENGTH,
            "Segment length (km)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=100.0,
            minValue=5))
        self.addParameter(QgsProcessingParameterNumber(
            self.SEGMENT_COUNT,
            "Number of segments",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=5,
            minValue=1))

        # --- Imagery parameters ---
        self.addParameter(QgsProcessingParameterNumber(
            self.BUFFER_METERS,
            "Buffer distance (m)",
            defaultValue=500,
            minValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.SENSOR,
            "Satellite Sensor",
            options=SENSOR_NAMES,
            defaultValue=0))  # Sentinel-2
        self.addParameter(QgsProcessingParameterDateTime(
            self.START_DATE,
            "Start Date",
            optional=False))
        self.addParameter(QgsProcessingParameterDateTime(
            self.END_DATE,
            "End Date",
            optional=False))
        self.addParameter(QgsProcessingParameterEnum(
            self.INDEX,
            "Index",
            options=INDEX_NAMES,
            allowMultiple=True,
            defaultValue=[0]))  # NDVI
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_CLOUD,
            "Max Cloud Cover (%)",
            defaultValue=20,
            minValue=0,
            maxValue=100))
        self.addParameter(QgsProcessingParameterEnum(
            self.STATS,
            "Statistics",
            options=STAT_NAMES,
            allowMultiple=True,
            defaultValue=[0, 1]))  # Mean + Median

        # --- Output ---
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT,
            "Segmented Reach Analysis"))

    def processAlgorithm(self, parameters, context, feedback):

        feedback.pushInfo("=== Phase 1: Selecting river by ID ===")

        river_source = self.parameterAsSource(parameters, self.INPUT, context)
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        id_field = self.parameterAsString(parameters, self.ID_FIELD, context)
        river_id = self.parameterAsString(parameters, self.RIVER_ID, context)

        if river_source is None:
            raise QgsProcessingException("No river layer provided.")

        river_feature = None
        for feature in river_source.getFeatures():
            if str(feature[id_field]) == str(river_id):
                river_feature = feature
                break

        if river_feature is None:
            raise QgsProcessingException(
                f"No river found with {id_field} = '{river_id}'. "
                "Check the field name and ID value."
            )

        river_geom = QgsGeometry(river_feature.geometry())

        source_crs = river_source.sourceCrs()
        transform = QgsCoordinateTransform(source_crs, crs, context.transformContext())
        river_geom.transform(transform)

        feedback.pushInfo(f"Found river {id_field}='{river_id}'")
        river_geom = ensure_northern_start(river_geom)



        feedback.pushInfo("=== Phase 2: Splitting river into segments ===")

        segment_mode = self.parameterAsEnum(parameters, self.SEGMENT_MODE, context)

        func = QgsDistanceArea()
        func.setSourceCrs(crs, context.transformContext())
        func.setEllipsoid(crs.ellipsoidAcronym())

        total_length_m = func.measureLength(river_geom)
        feedback.pushInfo(f"Total river length: {total_length_m / 1000:.4f} km")

        if segment_mode == 0:
            # "Length (km)" mode — user specifies the segment length.
            segment_length_km = self.parameterAsDouble(
                parameters, self.SEGMENT_LENGTH, context)
            segment_length_m = segment_length_km * 1000
        else:
            # "Number of segments" mode — user specifies count.
            # Derive segment length by dividing total length evenly.
            n_segments = self.parameterAsInt(
                parameters, self.SEGMENT_COUNT, context)
            if n_segments < 1:
                raise QgsProcessingException(
                    "Number of segments must be at least 1.")
            segment_length_m = total_length_m / n_segments

        feedback.pushInfo(
            f"Segment length: {segment_length_m:.0f} m")


        mem_uri = f"LineString?crs={crs.authid()}&field=temp_id:integer"
        single_layer = QgsVectorLayer(mem_uri, "single_river", "memory")
        if not single_layer.isValid():
            raise QgsProcessingException(
                "Failed to create temporary layer for splitting.")

        single_feat = QgsFeature(single_layer.fields())
        single_feat.setGeometry(river_geom)
        single_feat.setAttributes([1])
        single_layer.dataProvider().addFeature(single_feat)

        segment_layer = split_river_by_length(
            single_layer, segment_length_m, context, feedback)

        if segment_layer is None or segment_layer.featureCount() == 0:
            raise QgsProcessingException(
                "Splitting produced no segments. "
                "Check that the river geometry is valid.")

        total_segments = segment_layer.featureCount()
        feedback.pushInfo(f"Created {total_segments} segments")


        feedback.pushInfo("=== Phase 3: Preparing imagery and output ===")

        buffer_m = self.parameterAsDouble(parameters, self.BUFFER_METERS, context)
        sensor_idx = self.parameterAsEnum(parameters, self.SENSOR, context)
        start_date = parameters[self.START_DATE].toString("yyyy-MM-dd")
        end_date = parameters[self.END_DATE].toString("yyyy-MM-dd")
        index_idxs = self.parameterAsEnums(parameters, self.INDEX, context)
        stat_idxs = self.parameterAsEnums(parameters, self.STATS, context)
        max_cloud = self.parameterAsDouble(parameters, self.MAX_CLOUD, context)

        sensor_name = SENSOR_NAMES[sensor_idx]
        sensor_cfg = SENSOR_CONFIG[sensor_name]
        bands = sensor_cfg["bands"]
        collection_id = sensor_cfg["collection"]
        cloud_prop = sensor_cfg["cloud_property"]

        index_names = [INDEX_NAMES[i] for i in index_idxs]
        stat_names = [STAT_NAMES[i] for i in stat_idxs]

        if not index_names:
            raise QgsProcessingException("Select at least one index.")
        if not stat_names:
            raise QgsProcessingException("Select at least one statistic.")

        feedback.pushInfo(
            f"Sensor: {sensor_name}, Indices: {index_names}, Stats: {stat_names}")

        stat_field_keys = []
        for idx_name in index_names:
            for s_name in stat_names:
                suffix = STAT_EE_SUFFIX[s_name]
                stat_field_keys.append(f"{idx_name}_{suffix}")

        safe_stat_keys = [
            k.replace("-", "_").replace(" ", "_") for k in stat_field_keys
        ]

        fields = QgsFields()
        fields.append(QgsField("segment_id", QVariant.Int))
        fields.append(QgsField("river_id", QVariant.String))
        fields.append(QgsField("crow_flies_km", QVariant.Double))
        fields.append(QgsField("river_length_km", QVariant.Double))
        fields.append(QgsField("sinuosity", QVariant.Double))
        for k in safe_stat_keys:
            fields.append(QgsField(k, QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            fields, QgsWkbTypes.LineString, crs)

        if sink is None:
            raise QgsProcessingException("Could not create output sink.")


        feedback.pushInfo("=== Phase 4: Processing segments ===")

        reducer = build_reducer(stat_names)

        for seg_idx, seg_feature in enumerate(segment_layer.getFeatures()):

            if feedback.isCanceled():
                break

            feedback.setProgress(int(seg_idx / total_segments * 100))
            feedback.pushInfo(f"--- Segment {seg_idx + 1}/{total_segments} ---")

            seg_geom = QgsGeometry(seg_feature.geometry())


            start_pt = seg_geom.vertexAt(0)
            n_coords = seg_geom.constGet().nCoordinates()
            end_pt = seg_geom.vertexAt(n_coords - 1)

            crow = func.measureLine(QgsPointXY(start_pt), QgsPointXY(end_pt)) / 1000  # km
            river_length = func.measureLength(seg_geom) / 1000   # km

            if crow == 0:
                feedback.pushWarning(
                    f"Segment {seg_idx + 1}: crow-flies distance is zero, "
                    "setting sinuosity to 0.")
                sinuosity = 0.0
            else:
                sinuosity = river_length / crow

            feedback.pushInfo(
                f"  crow={crow:.4f} km, river={river_length:.4f} km, "
                f"sinuosity={sinuosity:.4f}")

            river_ee_geom = qgs_geometry_to_ee_geometry(seg_geom, crs, context)
            aoi = river_ee_geom.buffer(buffer_m)

            ic = ee.ImageCollection(collection_id)
            ic = ic.filterDate(start_date, end_date)
            ic = ic.filterBounds(aoi)
            ic = ic.filter(ee.Filter.lte(cloud_prop, max_cloud))

            if sensor_name.startswith("Landsat"):
                ic = ic.map(scale_landsat_c2)


            for idx in index_idxs:
                fn_name = INDEX_NAMES[idx]
                fn = INDEX_FUNCTIONS[fn_name]
                ic = ic.map(lambda img, f=fn, b=bands: f(img, b))

            composite = ic.select(index_names).median()
            composite = composite.clip(aoi)

            stats = composite.reduceRegion(
                reducer=reducer,
                geometry=aoi,
                scale=sensor_cfg["native_scale"],
                maxPixels=1e9,
            ).getInfo()


            stats = stats or {}
            stat_values = []
            for key in stat_field_keys:
                val = stats.get(key)
                stat_values.append(round(val, 4) if val is not None else None)

            feedback.pushInfo(f"  Stats: {stats}")

            # --- Write the output feature ---
            out_feat = QgsFeature(fields)
            out_feat.setGeometry(seg_geom)
            out_feat.setAttributes([
                seg_idx + 1,
                str(river_id),
                round(crow, 4),
                round(river_length, 4),
                round(sinuosity, 4),
            ] + stat_values)

            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)


        feedback.pushInfo(
            f"=== Complete: {total_segments} segments processed ===")

        return {self.OUTPUT: dest_id}