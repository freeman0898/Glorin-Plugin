# -*- coding: utf-8 -*-
"""
GLORIN plugin — pull satellite imagery for a river section and compute
environmental indices (NDVI, NDWI, MNDWI).
 
Supports both Sentinel-2 and Landsat 5/7/8/9 collections.
"""

import logging
 
import ee
 
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterDateTime,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingOutputRasterLayer,
    QgsProcessingOutputString,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsGeometry,
    QgsProcessing,
    QgsWkbTypes
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

def vector_layer_to_ee_geometry(layer) -> ee.Geometry:
    """Convert a QGIS vector layer to an ee.Geometry (in EPSG:4326).
 
    Earth Engine requires geometries in WGS84 geographic coordinates. We
    reproject the layer's features to EPSG:4326, extract their coordinates,
    and build an ee.Geometry.
    """
    source_crs = layer.crs() or QgsCoordinateReferenceSystem("EPSG:4326")
    target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())
    
    import json
    
    geometries = []
    features_seen = 0
    features_w_geom = 0
    
    for feature in layer.getFeatures():
        features_seen += 1
        raw_geom = feature.geometry()
        if raw_geom is None or raw_geom.isEmpty():
            logger.warning(f"Feature {feature.id()} has no geometry, skipping.")
            continue
            
        geom = QgsGeometry(raw_geom)
        geom.transform(transform)
        geojson_dict = json.loads(geom.asJson())
        
        if geojson_dict is None or "coordinates" not in geojson_dict:
            logger.warning(f"Feature {feature.id()} produced invalid GeoJSON, skipping.")
            continue
        
        features_w_geom += 1
        geometries.append(geojson_dict)
        
    logger.info(f"Seen {features_seen} features, {features_w_geom} had valid geometry.")
 
    if not geometries:
        raise ValueError("River layer has no geometry to convert.")
    
    if len(geometries) == 1:
        return ee.Geometry(geometries[0])

    return ee.Geometry({"type": "GeometryCollection", "geometries": geomtries})

class PullRiverImageryAlgorithm(QgsProcessingAlgorithm):
    """
    Pull imagery (Sentinel-2 or Landsat) for a river section and compute
    environmental indices (NDVI / NDWI / MNDWI).
    """
 
    # --- QGIS-required metadata methods ---
 
    def name(self):
        return "pull_river_imagery"
 
    def displayName(self):
        return "Pull River Imagery (NDVI / NDWI)"
 
    def group(self):
        return "GLORIN"
 
    def groupId(self):
        return "glorin"
 
    def shortHelpString(self):
        return (
            "<b>Pull River Imagery</b><br>"
            "Filters a Sentinel-2 or Landsat image collection by date, cloud "
            "cover, and the geometry of a river vector layer, computes the "
            "selected environmental index (NDVI, NDWI, or MNDWI), composites "
            "with a median reducer, and adds the result to the map.<br><br>"
            "Intended for the GLORIN project — quantifying environmental "
            "factors over a manually selected river stretch."
        )
 
    def createInstance(self):
        return PullRiverImageryAlgorithm()

    # --- Parameter definitions ---
    def initAlgorithm(self, config):
        self.addParameter(QgsProcessingParameterVectorLayer(
        'river_layer',
        'River vector layer',
        [QgsProcessing.TypeVectorLine],))
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
        
    # --- The actual work ---
 
    def processAlgorithm(self, parameters, context, feedback):
        
        # Pull parameter values
        river_layer = self.parameterAsVectorLayer(parameters, "river_layer", context)
        buffer_m = self.parameterAsDouble(parameters, "buffer_meters", context)
        sensor_idx = self.parameterAsEnum(parameters, "sensor", context)
        start_date = parameters["start_date"].toString("yyyy-MM-dd")
        end_date = parameters["end_date"].toString("yyyy-MM-dd")
        index_idxs = self.parameterAsEnums(parameters, "index", context)
        stat_idxs = self.parameterAsEnums(parameters, "stats", context)
        max_cloud = self.parameterAsDouble(parameters, "max_cloud", context)
        
        # Look up sensor configuration
        sensor_name = SENSOR_NAMES[sensor_idx]
        sensor_cfg = SENSOR_CONFIG[sensor_name]
        bands = sensor_cfg["bands"]
        collection_id = sensor_cfg["collection"]
        cloud_prop = sensor_cfg["cloud_property"]
 
        # Look up index (multi-select)
        index_names = [INDEX_NAMES[i] for i in index_idxs]
        
        #Look up stats
        stat_names = [STAT_NAMES[i] for i in stat_idxs]

        # 1. Convert the river vector to an EE geometry and buffer it.
        feedback.pushInfo("Converting river geometry to Earth Engine...")
        river_geom = vector_layer_to_ee_geometry(river_layer)
        aoi = river_geom.buffer(buffer_m)

        # 2. Build and filter the image collection.
        feedback.pushInfo(f"Filtering {sensor_name} collection...")
        ic = ee.ImageCollection(collection_id)
        ic = ic.filterDate(start_date, end_date)
        ic = ic.filterBounds(aoi)
        ic = ic.filter(ee.Filter.lte(cloud_prop, max_cloud))
        
        # Scale Landsat SR before computing indices — Sentinel-2 doesn't need this
        if sensor_name.startswith("Landsat"):
            ic = ic.map(scale_landsat_c2)

        # 3. Compute every selected index, then composite.
        feedback.pushInfo(f"Computing {', '.join(index_names)} and compositing...")
        for idx in index_idxs:
            fn_name = INDEX_NAMES[idx]
            fn = INDEX_FUNCTIONS[fn_name]
            ic = ic.map(lambda img, f=fn, b=bands: f(img, b))   # early-bind!

        composite = ic.select(index_names).median()

        # 4. Clip to the buffered AOI.
        composite = composite.clip(aoi)

        
        # 5. Compute statistics over the buffered AOI.
        feedback.pushInfo(f"Computing {index_names} statistics over river section...")

        reducer = build_reducer(stat_names)

        stats = composite.reduceRegion(
            reducer=reducer,
            geometry=aoi,
            scale=sensor_cfg["native_scale"],
            maxPixels=1e9
        ).getInfo()

        # Clean up the dict — keys will be like "NDVI_mean", "NDVI_min", "NDVI_p25"
        result = {k: round(v, 4) for k, v in stats.items() if v is not None}

        feedback.pushInfo(f"Results: {result}")

        return {"STATS": str(result)}

