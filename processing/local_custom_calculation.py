# -*- coding: utf-8 -*-

"""
GLORIN plugin — custom raster calculator, LOCAL version (no Earth Engine).

Same behaviour as the original tool, but the satellite imagery comes from a
GeoTIFF (or any GDAL-readable raster) on the user's computer instead of a
Google Earth Engine image collection. 

Example expressions:

    nir - red                       (simple difference)
    (nir - red) / (nir + red)       (that's NDVI)
    2.5 * (nir - red) / (nir + 6*red - 7.5*blue + 1)   (EVI)
"""

import json
import logging
import math
import os
import tempfile

import numpy as np
from osgeo import gdal

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingParameterCrs,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsDistanceArea,
    QgsCoordinateTransform,
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

logger = logging.getLogger(__name__)

SEGMENT_MODES = [
    "Whole river (no segmentation)",
    "Split by length (km)",
    "Split by number of segments",
]


STAT_NAMES = [
    "Mean",
    "Median",
    "Variance",
    "Skewness",
    "Min",
    "Max",
    "25th Percentile",
    "75th Percentile",
]

STAT_SUFFIX = {
    "Mean":            "mean",
    "Median":          "median",
    "Variance":        "variance",
    "Skewness":        "skew",
    "Min":             "min",
    "Max":             "max",
    "25th Percentile": "p25",
    "75th Percentile": "p75",
}

DEFAULT_BAND_MAP = "blue=1,green=2,red=3,nir=4,swir1=5"



def validate_expression(expression: str, band_names: dict) -> None:
    """Check the expression references at least one known band name."""
    if not any(logical in expression for logical in band_names):
        raise QgsProcessingException(
            "Expression contains no recognised band names "
            f"({', '.join(band_names)})."
        )


def validate_expression_syntax(expression: str) -> None:
    """Reject anything that is not plain band math.

    The local evaluator uses Python's eval() over numpy arrays. The
    expression is user-typed, so before evaluating we restrict it to
    characters that can only form arithmetic: letters, digits, brackets,
    arithmetic operators, whitespace and a decimal point. This is stricter
    than the GEE version needed to be, because eval() runs on THIS machine.
    """
    allowed = set("abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "0123456789_+-*/(). ,")
    if not expression or set(expression) - allowed:
        bad = sorted(set(expression) - allowed)[:10]
        raise QgsProcessingException(
            "Expression contains characters that are not allowed in "
            f"band math: {' '.join(repr(c) for c in bad)}"
        )


def parse_band_map(band_map_str: str, raster_layer) -> dict:
    """Parse 'red=3,nir=4' into {'red': 3, 'nir': 4} (1-based band numbers)."""
    bands = {}
    for part in band_map_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise QgsProcessingException(
                f"Band map entry '{part}' is not in the form name=number."
            )
        logical, _, num = part.partition("=")
        logical, num = logical.strip(), num.strip()
        try:
            idx = int(num)
        except ValueError:
            raise QgsProcessingException(
                f"Band number for '{logical}' is not an integer: '{num}'."
            )
        if idx < 1:
            raise QgsProcessingException(
                f"Band numbers are 1-based; got {idx} for '{logical}'."
            )
        if raster_layer is not None and idx > raster_layer.bandCount():
            raise QgsProcessingException(
                f"Band {idx} (mapped to '{logical}') does not exist in "
                f"'{raster_layer.name()}' — it only has "
                f"{raster_layer.bandCount()} band(s)."
            )
        bands[logical] = idx
    if not bands:
        raise QgsProcessingException("Band map is empty.")
    return bands


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


# Local raster evaluation (replaces the ee.ImageCollection block)

def _skew(a: np.ndarray):
    """Skewness (third standardised moment) without scipy.

    QGIS's Python doesn't ship scipy, and Earth Engine's reducer gave us
    'skew' for free — locally we compute the formula ourselves.
    """
    a = a[~np.isnan(a)]
    if a.size < 3 or float(a.std()) == 0:
        return None
    return float(((a - a.mean()) ** 3).mean() / a.std() ** 3)


NAN_STATS = {
    "mean":     lambda a: float(np.nanmean(a)),
    "median":   lambda a: float(np.nanmedian(a)),
    "variance": lambda a: float(np.nanvar(a)),
    "min":      lambda a: float(np.nanmin(a)),
    "max":      lambda a: float(np.nanmax(a)),
    "p25":      lambda a: float(np.nanpercentile(a, 25)),
    "p75":      lambda a: float(np.nanpercentile(a, 75)),
}


def evaluate_local(raster_layer, band_map: dict, expression: str,
                   aoi_geom: QgsGeometry, aoi_crs, context, scale_reflectance=False) -> dict:
    """Evaluate the band expression on a local raster inside aoi_geom.

    Returns {stat_suffix: value} using the same keys the GEE version
    produced ('mean', 'median', 'p25', ...), or None if the AOI falls
    entirely outside the raster or has no valid pixels.

    How it works:
      1. Open the file with GDAL (local GeoTIFFs open fine — this is the
         same library QGIS itself uses).
      2. Reproject the buffer polygon from the working CRS into the
         RASTER's CRS. Note: unlike the GEE version, which had to convert
         everything to WGS84 GeoJSON, GDAL is happy to work in any CRS —
         we just have to match the raster's own CRS.
      3. Convert the polygon's bounding box to pixel coordinates, clamp it
         to the raster's extent, and read only that window (so a
         10,000x10,000 pixel scene still costs us only the river corridor).
      4. Rasterize the buffer polygon into a 1/0 mask — the AOI is a
         rounded buffer, not a rectangle, so we need to know which pixels
         are inside. This is the local equivalent of ee's geometry-aware
         reduceRegion.
      5. Load each mapped band into a numpy array, mask outside pixels and
         nodata to NaN, then eval() the expression with the band arrays as
         variables. '(nir - red) / (nir + red)' becomes one vectorised
         numpy operation over every pixel at once.
      6. Compute the requested statistics on the masked values.
    """
    source = raster_layer.source()
    ds = gdal.Open(source, gdal.GA_ReadOnly)
    if ds is None:
        raise QgsProcessingException(
            f"Could not open '{source}' with GDAL. The local calculator "
            "needs a local raster file (e.g. a downloaded GeoTIFF), not an "
            "Earth Engine tile layer."
        )

    gt = ds.GetGeoTransform()
    if gt is None:
        raise QgsProcessingException(
            f"'{source}' has no valid geotransform (not a georeferenced "
            "raster?).")

    # --- 2. buffer polygon -> raster CRS ---
    raster_crs = raster_layer.crs()
    if raster_crs is None or not raster_crs.isValid():
        # Fall back to the CRS GDAL reports for the file.
        wkt = ds.GetProjection()
        if wkt:
            raster_crs = QgsCoordinateReferenceSystem.fromWkt(wkt)
        if raster_crs is None or not raster_crs.isValid():
            raise QgsProcessingException(
                f"Could not determine the CRS of '{source}'. "
                "Re-save the raster with a defined CRS and try again.")
    tr = QgsCoordinateTransform(aoi_crs, raster_crs,
                                context.transformContext())
    geom = QgsGeometry(aoi_geom)
    geom.transform(tr)
    rect = geom.boundingBox()

    # --- 3. bounding box -> pixel window ---
    inv_gt = gdal.InvGeoTransform(gt)
    if inv_gt is None:
        raise QgsProcessingException(
            f"'{source}' has a non-invertible geotransform.")
    px0, py0 = gdal.ApplyGeoTransform(inv_gt, rect.xMinimum(), rect.yMaximum())
    px1, py1 = gdal.ApplyGeoTransform(inv_gt, rect.xMaximum(), rect.yMinimum())
    px0 = max(int(math.floor(px0)), 0)
    py0 = max(int(math.floor(py0)), 0)
    px1 = min(int(math.ceil(px1)), ds.RasterXSize)
    py1 = min(int(math.ceil(py1)), ds.RasterYSize)
    if px1 <= px0 or py1 <= py0:
        return None  # segment is outside the raster footprint
    xsize, ysize = px1 - px0, py1 - py0

    # --- 4. rasterize the buffer into a mask ---
    geojson_dict = json.loads(geom.asJson())
    if geojson_dict is None or "coordinates" not in geojson_dict:
        raise QgsProcessingException(
            "Buffered segment produced invalid GeoJSON.")

    tmp_path = None
    mask_ds = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".geojson", delete=False) as f:
            json.dump({"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {},
                 "geometry": geojson_dict}]}, f)
            tmp_path = f.name

        # Geotransform of the WINDOW (origin shifted to px0, py0) — the
        # mask must align exactly with the window we read, not the whole
        # raster.
        window_gt = (gt[0] + px0 * gt[1], gt[1], gt[2],
                     gt[3] + py0 * gt[5], gt[4], gt[5])
        mask_ds = gdal.GetDriverByName("MEM").Create(
            "", xsize, ysize, 1, gdal.GDT_Byte)
        mask_ds.SetGeoTransform(window_gt)
        mask_ds.SetProjection(ds.GetProjection())
        gdal.Rasterize(mask_ds, tmp_path, burnValues=[1])
        inside = mask_ds.ReadAsArray() == 1
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not inside.any():
        return None

    # --- 5. load bands, apply mask + nodata, evaluate ---
    arrays = {}
    for logical, band_num in band_map.items():
        band = ds.GetRasterBand(band_num)
        nodata = band.GetNoDataValue()
        a = band.ReadAsArray(px0, py0, xsize, ysize)
        if scale_reflectance:
            a = a.astype("float32") * 0.0000275 - 0.2
        if a is None:
            raise QgsProcessingException(
                f"Failed to read band {band_num} ('{logical}') from "
                f"'{source}'.")
        a = a.astype(np.float32)
        if nodata is not None:
            a[a == nodata] = np.nan
        arrays[logical] = np.where(inside, a, np.nan)

    try:
        # validate_expression_syntax() has already restricted the string
        # to arithmetic characters, and validate_expression() has checked
        # it names at least one band. Empty __builtins__ keeps eval from
        # reaching anything else.
        result = eval(expression, {"__builtins__": {}}, arrays)
    except ZeroDivisionError:
        # numpy normally emits inf/NaN rather than raising, but a plain
        # scalar division would land here.
        raise QgsProcessingException(
            f"Expression divides by zero: {expression}")
    except Exception as e:
        raise QgsProcessingException(
            f"Could not evaluate expression '{expression}': {e}")

    result = np.asarray(result, dtype=np.float64)
    if result.shape != inside.shape:
        # An expression reducing to a scalar (e.g. 'nir / 0' style cases)
        # would give a single number; broadcast it so the mask still works.
        result = np.full(inside.shape, float(result), dtype=np.float64)

    vals = result[inside]
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return None

    # --- 6. statistics ---
    stats = {key: fn(vals) for key, fn in NAN_STATS.items()}
    stats["skew"] = _skew(vals)
    return stats


class LocalCustomRasterCalculatorAlgorithm(QgsProcessingAlgorithm):

    # --- Parameter names ---
    INPUT = "INPUT"
    CRS = "CRS"
    ID_FIELD = "ID_FIELD"
    RIVER_ID = "RIVER_ID"
    SEGMENT_MODE = "SEGMENT_MODE"
    SEGMENT_LENGTH = "SEGMENT_LENGTH"
    SEGMENT_COUNT = "SEGMENT_COUNT"
    BUFFER_METERS = "BUFFER_METERS"
    RASTER = "RASTER"
    SCALE_REFLECTANCE = "SCALE_REFLECTANCE"
    BAND_MAP = "BAND_MAP"
    EXPRESSION = "EXPRESSION"
    STATS = "STATS"
    OUTPUT = "OUTPUT"

    def name(self):
        return "local_custom_raster_calculator"

    def displayName(self):
        return "Custom Raster Calculator (Local Imagery)"

    def group(self):
        return "GLORIN"

    def groupId(self):
        return "glorin"

    def shortHelpString(self):
        return (
            "<b>Custom Raster Calculator (Local Imagery)</b><br>"
            "Pick a river from a line layer by its ID, optionally split it "
            "into segments, choose a <b>downloaded</b> satellite image "
            "(GeoTIFF), tell the tool which band numbers hold which logical "
            "bands, and type a band-math expression. Everything is computed "
            "on your own machine — <b>no Earth Engine sign-in and no "
            "internet connection are needed</b>. Sinuosity, crow-flies "
            "distance and river length are computed per segment. Examples:"
            "<br>"
            "<code>(nir - red) / (nir + red)</code> (NDVI)<br>"
            "<code>2.5 * (nir - red) / (nir + 6*red - 7.5*blue + 1)</code> "
            "(EVI)<br><br>"
            "Output is one feature per segment with its geometry and all "
            "statistics as attributes. River segments start counting from "
            "the northern vertex of the river and move towards the "
            "southern vertex."
        )

    def createInstance(self):
        return LocalCustomRasterCalculatorAlgorithm()

    def initAlgorithm(self, config):

        # --- River selection ---
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT,
            "River line layer",
            [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS,
            "Working CRS (leave unset to use the river layer's CRS) (UTM recommended)",
            optional=True))
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
            defaultValue=0))  # whole river
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

        # --- Imagery parameters (local) ---
        self.addParameter(QgsProcessingParameterNumber(
            self.BUFFER_METERS,
            "Buffer distance (m)",
            defaultValue=500,
            minValue=0))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.RASTER,
            "Local satellite imagery (GeoTIFF)",
            optional=False))
        self.addParameter(QgsProcessingParameterString(
            self.BAND_MAP,
            "Band map (1-based), e.g. blue=1,green=2,red=3,nir=4,swir1=5",
            defaultValue=DEFAULT_BAND_MAP,
            optional=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SCALE_REFLECTANCE,
            "Apply reflectance scaling (DN × 0.0000275 − 0.2)",
            defaultValue=False))
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
            "Local Calculator Results (one feature per segment)"))

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
        raster_layer = self.parameterAsRasterLayer(parameters, self.RASTER, context)
        band_map_str = self.parameterAsString(parameters, self.BAND_MAP, context).strip()
        scale_reflectance = self.parameterAsBoolean(parameters, self.SCALE_REFLECTANCE, context)
        expression = self.parameterAsString(parameters, self.EXPRESSION, context).strip()
        stat_idxs = self.parameterAsEnums(parameters, self.STATS, context)
        stat_names = [STAT_NAMES[i] for i in stat_idxs]

        if river_layer is None:
            raise QgsProcessingException("No river layer provided.")
        if raster_layer is None:
            raise QgsProcessingException("No imagery file provided.")
        if not expression:
            raise QgsProcessingException("No expression provided.")
        if not stat_names:
            raise QgsProcessingException("Select at least one statistic.")

        if not crs.isValid():
            crs = river_layer.sourceCrs()
            feedback.pushInfo(f"Using input layer CRS: {crs.authid()}")

        if crs.isGeographic():
            feedback.pushWarning(
                f"{crs.authid()} is geographic — lengths will be in degrees. "
                "Pick a projected CRS (e.g. the local UTM zone) for "
                "meaningful sinuosity.")

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

        band_map = parse_band_map(band_map_str, raster_layer)

        feedback.pushInfo(
            f"Imagery: {raster_layer.name()} "
            f"({raster_layer.bandCount()} bands)")
        feedback.pushInfo(f"Band map: {band_map}")
        
        feedback.pushInfo(
            "Reflectance scaling: "
            + ("APPLIED (DN × 0.0000275 − 0.2)" if scale_reflectance else "OFF"))

        validate_expression_syntax(expression)
        validate_expression(expression, band_map)
        feedback.pushInfo(f"Evaluating: {expression}")

        RESULT_KEYS = [f"RESULT_{STAT_SUFFIX[s]}" for s in stat_names]
        SAFE_RESULT_KEYS = [k.lower() for k in RESULT_KEYS]

        # --- Output sink ---
        out_fields = QgsFields()
        out_fields.append(QgsField("seg_id", QVariant.Int))
        out_fields.append(QgsField("river_id", QVariant.String))
        out_fields.append(QgsField("crow_km", QVariant.Double))
        out_fields.append(QgsField("river_length_km", QVariant.Double))
        out_fields.append(QgsField("sinuosity", QVariant.Double))
        out_fields.append(QgsField("expression", QVariant.String))
        for key in RESULT_KEYS:
            out_fields.append(QgsField(key, QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, out_fields,
            QgsWkbTypes.LineString, crs)

        if sink is None:
            raise QgsProcessingException("Failed to create output sink.")

        feedback.pushInfo("=== Phase 4: Per-segment analysis ===")

        all_results = {}
        skipped = 0

        for seg_idx, seg_feature in enumerate(segment_layer.getFeatures()):
            if feedback.isCanceled():
                return {}

            seg_geom = QgsGeometry(seg_feature.geometry())
            feedback.pushInfo(f"Segment {seg_idx + 1}/{total_segments}")

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

            buffered = seg_geom.buffer(buffer_m, 5)

            try:
                stats = evaluate_local(
                    raster_layer, band_map, expression,
                    buffered, crs, context,
                    scale_reflectance=scale_reflectance)
            except QgsProcessingException:
                raise
            except Exception as e:
                raise QgsProcessingException(
                    f"Local evaluation failed on segment {seg_idx + 1}: "
                    f"{e}\n"
                    f"Check the band map and expression syntax.")

            if stats is None:
                feedback.pushWarning(
                    f"Segment {seg_idx + 1}: no valid pixels inside the "
                    "buffer — is the imagery covering this part of the "
                    "river? Statistics set to NULL.")
                stats = {}
                skipped += 1

            stat_values = [
                round(stats[STAT_SUFFIX[s]], 4)
                if stats and stats.get(STAT_SUFFIX[s]) is not None
                else None
                for s in stat_names
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

        if skipped:
            feedback.pushWarning(
                f"{skipped} segment(s) had no valid pixels and were "
                "written with NULL statistics.")

        feedback.pushInfo(f"Output written to {dest_id}")
        return {self.OUTPUT: dest_id, "STATS": str(all_results)}
