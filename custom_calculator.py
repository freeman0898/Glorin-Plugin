# -*- coding: utf-8 -*-

""" GLORIN plugin — custom raster calculator (segmented, with sinuosity).

Lets the user type their own band-math expression (like QGIS raster calculator
syntax) and evaluates it in Earth Engine over a single river (picked by ID
field) — either as one whole stretch or split into segments — alongside
per-segment sinuosity. Example expressions:

    nir - red                       (simple difference)
    (nir - red) / (nir + red)       (that's NDVI)
    2.5 * (nir - red) / (nir + 6*red - 7.5*blue + 1)   (EVI)

Band names are resolved per sensor from SENSOR_CONFIG, so the same expression
works for Sentinel-2 and Landsat. Output is one feature PER SEGMENT with the
segment geometry, crow-flies distance, river length, sinuosity, the expression
text and its statistics as attributes.
"""

import json
import logging

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
    QgsProcessingOutputString,
)

from PyQt5.QtCore import QVariant
from qgis import processing


from .manual_segment_analysis import (
    SENSOR_CONFIG,
    SENSOR_NAMES,
    STAT_OPTIONS,
    STAT_NAMES,
    build_reducer,
    scale_landsat_c2,
)

logger = logging.getLogger(__name__)



SEGMENT_MODES = ["Whole river (no segmentation)",
                 "Split by length (km)",
                 "Split by number of segments"]


STAT_EE_SUFFIX = {
    "Mean":            "mean",
    "Median":          "median",
    "Variance":        "variance",
    "Skewness":        "skew",
    "Min":             "min",
    "Max":             "max",
    "25th Percentile": "p25",
    "75th Percentile": "p75",
}


def validate_expression(expression: str, band_names: dict) -> None:
    """Check the expression references at least one known band name."""
    
    if not any(logical in expression for logical in band_names):
        raise QgsProcessingException(
            "Expression contains no recognised band names "
            f"({', '.join(band_names)})."
        )


def qgs_geometry_to_ee_geometry(geom, source_crs, context) -> ee.Geometry:
    """Convert a QgsGeometry to an ee.Geometry (EPSG:4326).

    Earth Engine requires coordinates in WGS84 geographic. We transform the
    geometry, serialise it to GeoJSON, and pass it to ee.Geometry. Working
    on a copy ensures the original is not mutated.
    """
    target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(
        source_crs, target_crs, context.transformContext())

    geom = QgsGeometry(geom)
    geom.transform(transform)

    geojson_dict = json.loads(geom.asJson())
    if geojson_dict is None or "coordinates" not in geojson_dict:
        raise QgsProcessingException(
            "Selected river feature produced invalid GeoJSON.")
    return ee.Geometry(geojson_dict)


def find_river_feature(layer, id_field: str, river_id: str):
    """Return the first feature whose ID field matches river_id."""
    
    for feature in layer.getFeatures():
        if str(feature[id_field]) == str(river_id):
            return feature
    return None


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
    feature is a LineString of roughly segment_length_m metres; the last
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


class CustomRasterCalculatorAlgorithm(QgsProcessingAlgorithm):
    """User-defined raster calculator executed by Earth Engine, per segment,
    with sinuosity."""
    
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
    EXPRESSION = "EXPRESSION"
    MAX_CLOUD = "MAX_CLOUD"
    STATS = "STATS"
    OUTPUT = "OUTPUT"

    def name(self):
        return "custom_raster_calculator"

    def displayName(self):
        return "Custom Raster Calculator"

    def group(self):
        return "GLORIN"

    def groupId(self):
        return "glorin"

    def shortHelpString(self):
        return (
            "<b>Custom Raster Calculator</b><br>"
            "Pick a river from a line layer by its ID, optionally split it "
            "into segments, type a band-math expression using logical band "
            "names (blue, green, red, nir, swir1), and Earth Engine "
            "evaluates it over each segment's buffer. Sinuosity, crow-flies "
            "distance and river length are computed per segment. Examples:"
            "<br>"
            "<code>(nir - red) / (nir + red)</code> (NDVI)<br>"
            "<code>2.5 * (nir - red) / (nir + 6*red - 7.5*blue + 1)</code> "
            "(EVI)<br><br>"
            "Output is one feature per segment with its geometry and all "
            "statistics as attributes. 'Whole river' mode reproduces the "
            "old single-row behaviour. River segments start counting from"
            "the northern vertex of the river and move towards the southern"
            "vertex"
        )

    def createInstance(self):
        return CustomRasterCalculatorAlgorithm()

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
            "Segmentation mode",
            options=SEGMENT_MODES,
            defaultValue=0))  # whole river = old behaviour
        self.addParameter(QgsProcessingParameterNumber(
            self.SEGMENT_LENGTH,
            "Segment length (km)",
            defaultValue=100,
            minValue=5))
        self.addParameter(QgsProcessingParameterNumber(
            self.SEGMENT_COUNT,
            "Number of segments",
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
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_CLOUD,
            "Max Cloud Cover (%)",
            defaultValue=20,
            minValue=0,
            maxValue=100))
        self.addParameter(QgsProcessingParameterString(
            self.EXPRESSION,
            "Band expression (e.g. (nir - red) / (nir + red))",
            multiLine=True,
            optional=False))
        self.addParameter(QgsProcessingParameterEnum(
            self.STATS,
            "Statistics",
            options=STAT_NAMES,
            allowMultiple=True,
            defaultValue=[0, 1]))  # Mean + Median

        # --- Output ---
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT,
            "Calculator Results (one feature per segment)"))

        self.addOutput(QgsProcessingOutputString(
            "STATS", "Expression Statistics"))

    def processAlgorithm(self, parameters, context, feedback):

        feedback.pushInfo("=== Phase 1: Selecting river by ID ===")

        river_layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        id_field = self.parameterAsString(parameters, self.ID_FIELD, context)
        river_id = self.parameterAsString(parameters, self.RIVER_ID, context)
        segment_mode = self.parameterAsEnum(parameters, self.SEGMENT_MODE, context)
        buffer_m = self.parameterAsDouble(parameters, self.BUFFER_METERS, context)
        sensor_idx = self.parameterAsEnum(parameters, self.SENSOR, context)
        start_date = parameters[self.START_DATE].toString("yyyy-MM-dd")
        end_date = parameters[self.END_DATE].toString("yyyy-MM-dd")
        max_cloud = self.parameterAsDouble(parameters, self.MAX_CLOUD, context)
        expression = self.parameterAsString(parameters, self.EXPRESSION, context).strip()
        stat_idxs = self.parameterAsEnums(parameters, self.STATS, context)
        stat_names = [STAT_NAMES[i] for i in stat_idxs]

        if river_layer is None:
            raise QgsProcessingException("No river layer provided.")
        if not expression:
            raise QgsProcessingException("No expression provided.")
        if not stat_names:
            raise QgsProcessingException("Select at least one statistic.")

        # Reproject to the working CRS so all distance work happens in
        # metres. Geographic CRS would give degrees.
        reprojected = processing.run(
            "native:reprojectlayer",
            {
                "INPUT": river_layer,
                "TARGET_CRS": crs,
                "OUTPUT": "memory:",
            },
            context=context,
            is_child_algorithm=True,
        )
        river_layer = context.getMapLayer(reprojected["OUTPUT"])

        feedback.pushInfo(f"Looking up river {id_field}='{river_id}'...")
        river_feature = find_river_feature(river_layer, id_field, river_id)
        if river_feature is None:
            raise QgsProcessingException(
                f"No river found with {id_field} = '{river_id}'. "
                "Check the field name and ID value."
            )
        if (river_feature.geometry() is None
                or river_feature.geometry().isEmpty()):
            raise QgsProcessingException(
                "The selected river feature has no geometry.")

        river_geom = QgsGeometry(river_feature.geometry())
        river_geom = ensure_northern_start(river_geom)


        feedback.pushInfo("=== Phase 2: Segmentation ===")

        func = QgsDistanceArea()
        func.setSourceCrs(crs, context.transformContext())
        func.setEllipsoid(crs.ellipsoidAcronym())

        if segment_mode == 0:
            mem_uri = f"LineString?crs={crs.authid()}&field=temp_id:integer"
            segment_layer = QgsVectorLayer(mem_uri, "single_river", "memory")
            if not segment_layer.isValid():
                raise QgsProcessingException(
                    "Failed to create temporary layer.")
            feat = QgsFeature(segment_layer.fields())
            feat.setGeometry(river_geom)
            feat.setAttributes([1])
            segment_layer.dataProvider().addFeature(feat)
        else:
            total_length_m = func.measureLength(river_geom)
            feedback.pushInfo(
                f"Total river length: {total_length_m / 1000:.4f} km")

            if segment_mode == 1:
                # "Length (km)" mode — user specifies the segment length.
                segment_length_m = self.parameterAsDouble(
                    parameters, self.SEGMENT_LENGTH, context) * 1000
            else:
                # "Number of segments" mode — user specifies count.
                # Derive segment length by dividing total length evenly.
                n_segments = self.parameterAsInt(
                    parameters, self.SEGMENT_COUNT, context)
                if n_segments < 1:
                    raise QgsProcessingException(
                        "Number of segments must be at least 1.")
                segment_length_m = total_length_m / n_segments

            feedback.pushInfo(f"Segment length: {segment_length_m:.0f} m")

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
                "Segmentation produced no segments. "
                "Check that the river geometry is valid.")
        total_segments = segment_layer.featureCount()
        feedback.pushInfo(f"Created {total_segments} segment(s)")


        feedback.pushInfo("=== Phase 3: Preparing imagery and output ===")

        sensor_name = SENSOR_NAMES[sensor_idx]
        sensor_cfg = SENSOR_CONFIG[sensor_name]
        bands = sensor_cfg["bands"]
        collection_id = sensor_cfg["collection"]
        cloud_prop = sensor_cfg["cloud_property"]

        feedback.pushInfo(f"Sensor: {sensor_name}")
        feedback.pushInfo(f"Available bands: {', '.join(bands)}")

        validate_expression(expression, bands)
        feedback.pushInfo(f"Evaluating: {expression}")

        RESULT_KEYS = [f"RESULT_{STAT_EE_SUFFIX[s]}" for s in stat_names]

        SAFE_RESULT_KEYS = [
            k.replace("-", "_").replace(" ", "_") for k in RESULT_KEYS]
                            
                            
        out_fields = QgsFields()
        out_fields.append(QgsField("segment_id", QVariant.LongLong))
        out_fields.append(QgsField("river_id", QVariant.String))
        out_fields.append(QgsField("crow_km", QVariant.Double))
        out_fields.append(QgsField("river_length_km", QVariant.Double))
        out_fields.append(QgsField("sinuosity", QVariant.Double))
        out_fields.append(QgsField("expression", QVariant.String))
        for k in SAFE_RESULT_KEYS:
            out_fields.append(QgsField(k, QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, QgsWkbTypes.LineString, crs)
        if sink is None:
            raise QgsProcessingException("Could not create output sink.")

        reducer = build_reducer(stat_names)
        

        feedback.pushInfo("=== Phase 4: Processing segments ===")

        all_results = {}

        for seg_idx, seg_feature in enumerate(segment_layer.getFeatures()):

            if feedback.isCanceled():
                break

            feedback.setProgress(int(seg_idx / total_segments * 100))
            feedback.pushInfo(f"--- Segment {seg_idx + 1}/{total_segments} ---")

            seg_geom = QgsGeometry(seg_feature.geometry())

            start_pt = seg_geom.vertexAt(0)
            n_coords = seg_geom.constGet().nCoordinates()
            end_pt = seg_geom.vertexAt(n_coords - 1)

            crow_km = func.measureLine(QgsPointXY(start_pt), QgsPointXY(end_pt)) / 1000
            river_length_km = func.measureLength(seg_geom) / 1000

            if crow_km == 0:
                feedback.pushWarning(
                    f"Segment {seg_idx + 1}: crow-flies distance is zero, "
                    "setting sinuosity to 0.")
                sinuosity = 0.0
            else:
                sinuosity = river_length_km / crow_km

            feedback.pushInfo(
                f"  crow={crow_km:.4f} km, river={river_length_km:.4f} km, "
                f"sinuosity={sinuosity:.4f}")

            river_ee_geom = qgs_geometry_to_ee_geometry(
                seg_geom, crs, context)
            aoi = river_ee_geom.buffer(buffer_m)

            ic = ee.ImageCollection(collection_id)
            ic = ic.filterDate(start_date, end_date)
            ic = ic.filterBounds(aoi)
            ic = ic.filter(ee.Filter.lte(cloud_prop, max_cloud))

            if sensor_name.startswith("Landsat"):
                ic = ic.map(scale_landsat_c2)

            def evaluate(img):
                band_vars = {logical: img.select(real)
                             for logical, real in bands.items()}
                return img.expression(expression, band_vars) \
                    .rename("RESULT") \
                    .copyProperties(img, img.propertyNames())

            ic = ic.map(evaluate)

            composite = ic.select("RESULT").median().clip(aoi)

            try:
                stats = composite.reduceRegion(
                    reducer=reducer,
                    geometry=aoi,
                    scale=sensor_cfg["native_scale"],
                    maxPixels=1e9,
                ).getInfo()
            except ee.EEException as e:
                raise QgsProcessingException(
                    f"Earth Engine failed on segment {seg_idx + 1}: {e}\n"
                    f"Check band names ({', '.join(bands)}) and syntax."
                )

            stats = stats or {}
            stat_values = [
                round(stats[k], 4) if stats.get(k) is not None else None
                for k in RESULT_KEYS
            ]
            feedback.pushInfo(
                f"  Results: {dict(zip(SAFE_RESULT_KEYS, stat_values))}")
            all_results[seg_idx + 1] = dict(
                zip(SAFE_RESULT_KEYS, stat_values))

            # --- Write one output feature per segment ---
            attrs = [seg_idx + 1, str(river_id), crow_km, river_length_km,
                     sinuosity, expression] + stat_values
            if len(attrs) != out_fields.count():
                raise QgsProcessingException(
                    f"Attribute mismatch writing segment {seg_idx + 1}: "
                    f"{len(attrs)} values for {out_fields.count()} fields.")
            out_feature = QgsFeature(out_fields)
            out_feature.setGeometry(seg_geom)
            out_feature.setAttributes(attrs)
            if not sink.addFeature(out_feature, QgsFeatureSink.FastInsert):
                raise QgsProcessingException(
                    f"Failed to add feature for segment {seg_idx + 1}.")

        feedback.pushInfo(f"Output written to {dest_id}")
        return {self.OUTPUT: dest_id, "STATS": str(all_results)}
