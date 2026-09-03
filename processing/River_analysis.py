# -*- coding: utf-8 -*-
"""
GLORIN plugin — combined river analysis. This finds the sinuosity of a segment of river 
and then collects and calculates enviornmental indices based on satellite imagery pulled 
from Google Earth Engine. This is then written to a single output feature, optionally 
along with the river segment.
"""

import json
import logging
from typing import Any, Optional

import ee

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterPoint,
    QgsProcessingParameterCrs,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterDateTime,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterVectorLayer,
    QgsDistanceArea,
    QgsCoordinateTransform,
    QgsProject,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsWkbTypes,
    QgsProcessing,
    QgsCoordinateReferenceSystem,
    QgsProcessingOutputString,
    QgsFeatureSink,
    QgsProcessingParameterFileDestination,
    QgsVectorLayer,
    QgsVectorFileWriter,
)

from ..utils import (
    translate as _,
    add_or_update_ee_raster_layer,
    get_ee_properties,
    get_available_bands,
    filter_functions,
    get_ee_extent,
    parse_extent_string,
    normalize_crs,
)

from PyQt5.QtCore import QVariant
from qgis import processing

logger = logging.getLogger(__name__)

SENSOR_CONFIG = {
    "Sentinel-2 SR": {
        "collection": "COPERNICUS/S2_SR_HARMONIZED",
        "bands": {
            "green": "B3",
            "red": "B4",
            "nir": "B8",
            "swir1": "B11",
        },
        # Sentinel-2 stores cloud cover as CLOUDY_PIXEL_PERCENTAGE.
        "cloud_property": "CLOUDY_PIXEL_PERCENTAGE",
        # Native resolution in meters — used only for naming/logging.
        "native_scale": 10,
    },
    "Landsat 8/9 C2 L2": {
        "collection": "LANDSAT/LC09/C02/T1_L2",
        "bands": {
            "green": "SR_B3",
            "red": "SR_B4",
            "nir": "SR_B5",
            "swir1": "SR_B6",
        },
        "cloud_property": "CLOUD_COVER",
        "native_scale": 30,
    },
    "Landsat 7 C2 L2": {
        "collection": "LANDSAT/LE07/C02/T1_L2",
        "bands": {
            "green": "SR_B2",
            "red": "SR_B3",
            "nir": "SR_B4",
            "swir1": "SR_B5",
        },
        "cloud_property": "CLOUD_COVER",
        "native_scale": 30,
    },
    "Landsat 5 C2 L2": {
        "collection": "LANDSAT/LT05/C02/T1_L2",
        "bands": {
            "green": "SR_B2",
            "red": "SR_B3",
            "nir": "SR_B4",
            "swir1": "SR_B5",
        },
        "cloud_property": "CLOUD_COVER",
        "native_scale": 30,
    },
}
 
SENSOR_NAMES = list(SENSOR_CONFIG.keys())

#Index Functions

def scale_landsat_c2(image: ee.Image) -> ee.Image:
    """Apply Landsat Collection 2 Level-2 SR scaling.

    SR bands are stored as: reflectance = DN × 0.0000275 − 0.2
    Must be applied before computing indices, otherwise the offset
    distorts the normalized difference ratio.
    """
    # Optical SR bands follow the pattern SR_B1 through SR_B7
    optical = image.select("SR_B.*").multiply(0.0000275).add(-0.2)

    # Copy over non-SR bands (like QA_PIXEL) untouched
    return image.addBands(optical, overwrite=True)
    
def add_ndvi(image: ee.Image, bands: dict) -> ee.Image:
    """NDVI = (NIR - Red) / (NIR + Red).
 
    Vegetation vigour index. Values near +1 are dense canopy; near 0 or
    below are bare soil, water, or built-up area. For a river corridor you
    expect low NDVI over the channel and higher NDVI on the banks.
    """
    ndvi = image.normalizedDifference([bands["nir"], bands["red"]]).rename("NDVI")
    return image.addBands(ndvi)
 
 
def add_ndwi(image: ee.Image, bands: dict) -> ee.Image:
    """NDWI (McFeeters 1996) = (Green - NIR) / (Green + NIR).
 
    Highlights open water. Water absorbs NIR strongly, so the index is
    positive over water and approaches +1.
    """
    ndwi = image.normalizedDifference([bands["green"], bands["nir"]]).rename("NDWI")
    return image.addBands(ndwi)
 
 
def add_mndwi(image: ee.Image, bands: dict) -> ee.Image:
    """MNDWI (Xu 2006) = (Green - SWIR1) / (Green + SWIR1).
 
    Better than NDWI for separating water from built-up land. Uses the
    SWIR band, which on Sentinel-2 is 20 m and on Landsat is 30 m, so the
    composite will be at the coarser of the two resolutions.
    """
    mndwi = image.normalizedDifference([bands["green"], bands["swir1"]]).rename("MNDWI")
    return image.addBands(mndwi)

INDEX_FUNCTIONS = {
    "NDVI": add_ndvi,
    "NDWI": add_ndwi,
    "MNDWI": add_mndwi,
}
 
INDEX_NAMES = list(INDEX_FUNCTIONS.keys())

#Statistics Reducers

STAT_OPTIONS = {
    "Mean":              lambda: ee.Reducer.mean(),
    "Median":            lambda: ee.Reducer.median(),
    "Variance":          lambda: ee.Reducer.variance(),
    "Skewness":          lambda: ee.Reducer.skew(),
    "Min":               lambda: ee.Reducer.min(),
    "Max":               lambda: ee.Reducer.max(),
    "25th Percentile":   lambda: ee.Reducer.percentile([25]),
    "75th Percentile":   lambda: ee.Reducer.percentile([75]),
}

STAT_NAMES = list(STAT_OPTIONS.keys())


def build_reducer(selected_names: list) -> ee.Reducer:
    """Build a combined EE reducer from selected stat names.

    GEE reducers are chained with .combine() so a single reduceRegion
    call computes everything in one server-side pass.
    """
    if not selected_names:
        raise ValueError("No statistics selected.")

    reducer = None
    for name in selected_names:
        r = STAT_OPTIONS[name]()
        if reducer is None:
            reducer = r
        else:
            reducer = reducer.combine(r, sharedInputs=True)

    return reducer
    
#Geometry Converter

def qgs_geometry_to_ee_geometry(geom: QgsGeometry) -> ee.Geometry:
    """Convert a QGIS QgsGeometry to an ee.Geometry in EPSG:4326.

    Earth Engine requires geometries in WGS84 geographic coordinates.
    We reproject the geometry to EPSG:4326, serialise to GeoJSON, and
    build an ee.Geometry from it.

    This replaces the original vector_layer_to_ee_geometry, which
    iterated over an entire layer. Here we only need the path geometry
    that the shortest-path algorithm produced.
    """
    source_crs = QgsProject.instance().crs()
    target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())

    # Work on a copy so we don't mutate the original
    geom = QgsGeometry(geom)
    geom.transform(transform)

    geojson_dict = json.loads(geom.asJson())

    if geojson_dict is None or "coordinates" not in geojson_dict:
        raise ValueError("Path geometry produced invalid GeoJSON.")

    return ee.Geometry(geojson_dict)
    
class RiverAnalysisAlgorithm(QgsProcessingAlgorithm):
    """
    Pull imagery (Sentinel-2 or Landsat) for a river section and compute
    environmental indices (NDVI / NDWI / MNDWI).
    """

    INPUT = "INPUT"
    CRS = "CRS"
    START_POINT = "START_POINT"
    END_POINT = "END_POINT"
    BUFFER_METERS = "BUFFER_METERS"
    SENSOR = "SENSOR"
    START_DATE = "START_DATE"
    END_DATE = "END_DATE"
    INDEX = "INDEX"
    MAX_CLOUD = "MAX_CLOUD"
    STATS = "STATS"
    INCLUDE_GEOMETRY = "INCLUDE_GEOMETRY"
    OUTPUT = "OUTPUT"
    
    # --- QGIS-required metadata methods ---
 
    def name(self):
        return "river_analysis"
 
    def displayName(self):
        return "River Analysis (Sinuosity and Environmental Indices)"
 
    def group(self):
        return "GLORIN"
 
    def groupId(self):
        return "glorin"
 
    def shortHelpString(self):
        return (
            "<b>River Analysis</b><br>"
            "Select a river line layer and two points (start, end). "
            "The tool finds the path along the river between the points, "
            "computes sinuosity, then pulls Sentinel-2 or Landsat imagery "
            "for that stretch and calculates NDVI / NDWI / MNDWI statistics. "
            "Results are written to a single feature (saveable as CSV or "
            "shapefile). Optionally include the path geometry so the river "
            "segment is saved alongside the statistics.<br><br>"
            "Intended for the GLORIN project."
        )
 
    def createInstance(self):
        return RiverAnalysisAlgorithm()
        
    def initAlgorithm(self, config):
        
        #Sinuosity Inputs
        self.addParameter(QgsProcessingParameterFeatureSource(
        'INPUT',
        'Input layer'))
        self.addParameter(QgsProcessingParameterCrs(
        'CRS',
        'Input CRS (UTM)'))
        self.addParameter(QgsProcessingParameterPoint(
        'START POINT',
        'Start point',))
        self.addParameter(QgsProcessingParameterPoint(
        'END POINT',
        'End point',))
        
        #Imagery Inputs
        self.addParameter(QgsProcessingParameterNumber(
        'buffer_meters',
        'Buffer distance (m)',
        defaultValue=500,
        minValue=0))
        self.addParameter(QgsProcessingParameterEnum(
        'sensor',
        'Satellite Sensor',
        options=SENSOR_NAMES,
        defaultValue=0)) #Sentinel-2
        self.addParameter(QgsProcessingParameterDateTime(
        'start_date',
        'Start Date',
        optional=False))
        self.addParameter(QgsProcessingParameterDateTime(
        'end_date',
        'End Date',
        optional=False))
        self.addParameter(QgsProcessingParameterEnum(
        'index',
        'Index',
        options=INDEX_NAMES,
        allowMultiple=True,
        defaultValue=[0])) #default: NDVI
        self.addParameter(QgsProcessingParameterNumber(
        'max_cloud',
        'Max Cloud Cover (%)',
        defaultValue=20,
        minValue=0,
        maxValue=100))
        self.addParameter(QgsProcessingParameterEnum(
        'stats',
        'Statistics',
        options=STAT_NAMES,
        allowMultiple=True,
        defaultValue=[0, 1]))  # default: Mean + Median
        self.addOutput(QgsProcessingOutputString(
        "STATS", 
        "Index Statistcis"))
        
        #Output
        self.addParameter(QgsProcessingParameterFileDestination(
        self.OUTPUT,
        'River Analysis Results',
        'GeoPackage (*.gpkg);;Shapefile (*.shp);;CSV with WKT geometry (*.csv)'))
        
    def processAlgorithm(self, parameters, context, feedback):
        
        feedback.pushInfo("=== Phase 1: Sinuosity Calculation ===")
        
        river_layer = self.parameterAsLayer(parameters, 'INPUT', context)
        start_point = self.parameterAsPoint(parameters, 'START POINT', context)
        end_point = self.parameterAsPoint(parameters, 'END POINT', context)
        crs = self.parameterAsCrs(parameters, 'CRS', context)
        
        if river_layer is None:
            raise QgsProcessingException("No river layer provided.")
            
        reprojected = processing.run(
            'native:reprojectlayer',
            {
                'INPUT': river_layer,
                'TARGET_CRS': crs,
                'OUTPUT': 'memory:'},
            context=context,
            is_child_algorithm = True )
        river_layer = reprojected['OUTPUT']

        transform = QgsCoordinateTransform(
            QgsProject.instance().crs(),
            crs,
            context.transformContext(),
        )
        start_point = transform.transform(start_point)
        end_point = transform.transform(end_point)
        
        func = QgsDistanceArea()
        func.setSourceCrs(crs, context.transformContext())
        func.setEllipsoid(crs.ellipsoidAcronym())
        
        crow = func.measureLine(start_point, end_point) / 1000
        
        path_result = processing.run(
        'native:shortestpathpointtopoint',
        {
            'INPUT': river_layer,
            'START_POINT': start_point,
            'END_POINT': end_point,
            'STRATEGY': 0,
            'DIRECTION_FIELD': None,
            'VALUE_FORWARD': None,
            'VALUE_BACKWARD': None,
            'VALUE_BOTH': None,
            'DEFAULT_DIRECTION': 2,
            'SPEED_FIELD': None,
            'DEFAULT_SPEED': 5.0,
            'TOLERANCE': 100.0,
            'OUTPUT': 'memory:'
        },
        context=context,
        feedback=feedback,
        is_child_algorithm=True)

        path_layer = context.getMapLayer(path_result["OUTPUT"])

        if path_layer is None or path_layer.featureCount() == 0:
            raise QgsProcessingException(
                "No path found between the points. "
                "Check that the points are near the river network "
                "and that the lines are connected."
            )
            
        river_length = 0.0
        path_geom = None
        for feature in path_layer.getFeatures():
            geom = feature.geometry()
            river_length += func.measureLength(geom) / 1000
            # Keep the last geometry — for a single path there's
            # typically one feature. If multiple, we merge below.
            if path_geom is None:
                path_geom = QgsGeometry(geom)
            else:
                path_geom = path_geom.combine(QgsGeometry(geom))

        if path_geom is None or path_geom.isEmpty():
            raise QgsProcessingException("Path geometry is empty.")
            
        if crow == 0:
            raise QgsProcessingException(
                "Crow-flies distance is zero — start and end points "
                "are identical."
            )
        sinuosity = river_length / crow

        feedback.pushInfo(f"Crow-flies distance: {crow:.4f} km")
        feedback.pushInfo(f"River length: {river_length:.4f} km")
        feedback.pushInfo(f"Sinuosity: {sinuosity:.4f}")
        
        
        
        
        feedback.pushInfo("=== Phase 2: GEE Environmental Indices ===")
        
        buffer_m = self.parameterAsDouble(parameters, "buffer_meters", context)
        sensor_idx = self.parameterAsEnum(parameters, "sensor", context)
        start_date = parameters["start_date"].toString("yyyy-MM-dd")
        end_date = parameters["end_date"].toString("yyyy-MM-dd")
        index_idxs = self.parameterAsEnums(parameters, "index", context)
        stat_idxs = self.parameterAsEnums(parameters, "stats", context)
        max_cloud = self.parameterAsDouble(parameters, "max_cloud", context)

        sensor_name = SENSOR_NAMES[sensor_idx]
        sensor_cfg = SENSOR_CONFIG[sensor_name]
        bands = sensor_cfg["bands"]
        collection_id = sensor_cfg["collection"]
        cloud_prop = sensor_cfg["cloud_property"]

        index_names = [INDEX_NAMES[i] for i in index_idxs]
        stat_names = [STAT_NAMES[i] for i in stat_idxs]
        
        feedback.pushInfo("Converting path geometry to Earth Engine...")
        river_geom = qgs_geometry_to_ee_geometry(path_geom)
        aoi = river_geom.buffer(buffer_m)
        
        ic = ee.ImageCollection(collection_id)
        ic = ic.filterDate(start_date, end_date)
        ic = ic.filterBounds(aoi)
        ic = ic.filter(ee.Filter.lte(cloud_prop, max_cloud))
        
        if sensor_name.startswith("Landsat"):
            ic = ic.map(scale_landsat_c2)
            
        feedback.pushInfo(f"Computing {', '.join(index_names)} and compositing...")
        for idx in index_idxs:
            fn_name = INDEX_NAMES[idx]
            fn = INDEX_FUNCTIONS[fn_name]
            ic = ic.map(lambda img, f=fn, b=bands: f(img, b))    
            
        composite = ic.select(index_names).median()
        composite = composite.clip(aoi)
        
        feedback.pushInfo(f"Computing {index_names} statistics over river section...")

        reducer = build_reducer(stat_names)

        stats = composite.reduceRegion(
            reducer=reducer,
            geometry=aoi,
            scale=sensor_cfg["native_scale"],
            maxPixels=1e9,
        ).getInfo()
        
        result = {k: round(v, 4) for k, v in stats.items() if v is not None}
        
        feedback.pushInfo(f"Results: {result}")
        
        
        
        feedback.pushInfo("=== Phase 3: Writing output ===")

        dest_path = self.parameterAsString(parameters, self.OUTPUT, context)
        is_csv = dest_path.lower().endswith(".csv")

        stat_keys = sorted(result.keys())
        stat_values = [result[k] for k in stat_keys]
        safe_keys = [k.replace("-", "_").replace(" ", "_") for k in stat_keys]

        if is_csv:
            # --- direct CSV write: full control of column order and quoting ---
            import csv
            with open(dest_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["crow_flies", "river_length", "sinuosity"]
                                + safe_keys + ["WKT"])          # geometry last
                writer.writerow([crow, river_length, sinuosity]
                                + stat_values
                                + [path_geom.asWkt()])          # one quoted cell

            feedback.pushInfo(f"Output written to {dest_path}")
            return {self.OUTPUT: dest_path}

        # --- vector formats: real geometry via the memory-layer path ---
        field_names = ["crow_flies", "river_length", "sinuosity"] + safe_keys

        fields = QgsFields()
        for name in field_names:
            fields.append(QgsField(name, QVariant.Double))

        field_defs = "&".join(f"field={n}:double" for n in field_names)
        uri = (f"{QgsWkbTypes.displayString(path_geom.wkbType())}"
               f"?crs={crs.authid()}&{field_defs}")

        mem_layer = QgsVectorLayer(uri, "river_analysis", "memory")
        if not mem_layer.isValid():
            raise QgsProcessingException("Failed to create memory output layer.")

        out_feature = QgsFeature(mem_layer.fields())
        if not out_feature.setAttributes([crow, river_length, sinuosity] + stat_values):
            raise QgsProcessingException("Attribute mismatch writing output feature.")
        out_feature.setGeometry(path_geom)

        if not mem_layer.dataProvider().addFeature(out_feature):
            raise QgsProcessingException("Failed to add feature to memory layer.")
        mem_layer.updateFields()

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = QgsVectorFileWriter.driverForExtension(
            dest_path.split(".")[-1])
        options.fileEncoding = "UTF-8"

        error = QgsVectorFileWriter.writeAsVectorFormatV3(
            mem_layer, dest_path, context.transformContext(), options)

        if error[0] != QgsVectorFileWriter.NoError:
            raise QgsProcessingException(f"Error writing output: {error[1]}")

        feedback.pushInfo(f"Output written to {dest_path}")
        return {self.OUTPUT: dest_path}