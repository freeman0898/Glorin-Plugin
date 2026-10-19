# -*- coding: utf-8 -*-

"""
GLORIN plugin — sinuosity by segment (simplified).

Takes a river layer, a working CRS, and a segment count. Each river
feature in the layer is reprojected to the working CRS, normalised
so it always starts at its northern end, split into N equal-length
segments, and the sinuosity of each segment is computed and written
to the output sink.

The CRS parameter is optional: if left empty, the river layer's own
CRS is used (provided it is projected — geographic CRSs are
rejected, because degree-based distances would make the results
meaningless).

This is the "sinuosity only" sibling of segmented_reach_analysis.py:
no Earth Engine, no imagery, no ID selection — pure geometry,
so it runs in seconds and needs no authentication.

Direction normalisation and splitting reuse the shared helpers from
segmented_reach_analysis, so every tool numbers segments the same
way — one source of truth.

Intended for the GLORIN project — quantifying how large river
sinuosity varies along the length of a river.
"""

import logging
from typing import Any, Optional

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField, 
    QgsProcessingParameterString, 
    QgsProcessingParameterCrs,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsDistanceArea,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsProcessing,
    QgsVectorLayer,
    QgsFeatureSink,
)
from PyQt5.QtCore import QVariant
from qgis import processing

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
    
def find_river_feature(layer, id_field: str, river_id: str):
    """Return the first feature whose ID field matches river_id."""
    for feature in layer.getFeatures():
        if str(feature[id_field]) == str(river_id):
            return feature
    return None

SEGMENT_MODES = ["Whole river (no segmentation)",
                 "Split by length (km)",
                 "Split by number of segments"]
                 
logger = logging.getLogger(__name__)


class SinuosityBySegmentAlgorithm(QgsProcessingAlgorithm):
    """
    Split every river feature into N equal-length segments and
    compute sinuosity per segment.
    """

    # --- Parameter names ---
    INPUT = "INPUT"
    CRS = "CRS"
    ID_FIELD = "ID_FIELD"
    RIVER_ID = "RIVER_ID"
    SEGMENT_MODE = "SEGMENT_MODE"
    SEGMENT_LENGTH = "SEGMENT_LENGTH"
    SEGMENT_COUNT = "SEGMENT_COUNT"
    OUTPUT = "OUTPUT"

    # --- QGIS-required metadata methods ---

    def name(self) -> str:
        return "sinuosity_by_segment"

    def displayName(self) -> str:
        return "Sinuosity by Segment"

    def group(self) -> str:
        return "GLORIN"

    def groupId(self) -> str:
        return "glorin"

    def createInstance(self) -> "SinuosityBySegmentAlgorithm":
        return SinuosityBySegmentAlgorithm()

    def shortHelpString(self) -> str:
        return (
            "This tool splits each river in the input layer into a "
            "fixed number of equal-length segments and computes the "
            "sinuosity of every segment. Lines are first normalised "
            "so every river starts at its northern end — segment 1 "
            "is always the northernmost stretch, regardless of how "
            "the river was originally digitised. If the CRS parameter "
            "is left empty, the river layer's own CRS is used — but "
            "it must be a projected CRS (e.g. UTM), because distances "
            "must be measured in metres. Output is one feature per "
            "segment with its geometry and sinuosity as attributes."
        )

    def initAlgorithm(self, config: Optional[dict[str, Any]] = None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT,
            "River layer",
            [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS,
            "Working CRS (leave empty to use the river "
            "layer's CRS) (UTM recommended)",
            optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.ID_FIELD,
            "River ID field",
            parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterString(
            self.RIVER_ID,
            "River ID value"))
            
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
            "Number of segments per river",
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=10,
            minValue=1))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT,
            "Output"))

    def processAlgorithm(self, parameters, context, feedback):
        river_layer = self.parameterAsLayer(parameters, self.INPUT, context)
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        id_field = self.parameterAsString(parameters, self.ID_FIELD, context)
        river_id = self.parameterAsString(parameters, self.RIVER_ID, context)
        segment_mode = self.parameterAsEnum(parameters, self.SEGMENT_MODE, context)


        if river_layer is None:
            raise QgsProcessingException("No river layer provided.")

        if not crs.isValid():
            crs = river_layer.sourceCrs()
            feedback.pushInfo(
                f"CRS not set — using the river layer's CRS: "
                f"{crs.authid()}")

        if crs.isGeographic():
            raise QgsProcessingException(
                f"The working CRS ({crs.authid()}) is geographic — "
                "its units are degrees, so lengths and sinuosity "
                "would be meaningless. Choose a projected CRS "
                "(e.g. a UTM zone) instead.")
            
                

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
                "Check the field name and ID value.")
        if (river_feature.geometry() is None
                or river_feature.geometry().isEmpty()):
            raise QgsProcessingException(
                "The selected river feature has no geometry.")

        river_geom = QgsGeometry(river_feature.geometry())
        river_geom = ensure_northern_start(river_geom)

        func = QgsDistanceArea()
        func.setSourceCrs(crs, context.transformContext())
        func.setEllipsoid(crs.ellipsoidAcronym())

        feedback.pushInfo("=== Phase 2: Segmentation ===")

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
                segment_length_m = self.parameterAsDouble(
                    parameters, self.SEGMENT_LENGTH, context) * 1000
            else:
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

        # --- Output schema ---
        fields = QgsFields()
        fields.append(QgsField("segment_id", QVariant.Int))
        fields.append(QgsField("river_id", QVariant.String))
        fields.append(QgsField("crow_flies_km", QVariant.Double))
        fields.append(QgsField("river_length_km", QVariant.Double))
        fields.append(QgsField("sinuosity", QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            fields, QgsWkbTypes.LineString, crs)

        if sink is None:
            raise QgsProcessingException("Could not create output sink.")


        for seg_idx, seg_feature in enumerate(segment_layer.getFeatures()):

            if feedback.isCanceled():
                break

            feedback.pushInfo(
                f"--- Segment {seg_idx + 1}/{total_segments} ---")

            seg_geom = QgsGeometry(seg_feature.geometry())

            start_pt = seg_geom.vertexAt(0)
            n_coords = seg_geom.constGet().nCoordinates()
            end_pt = seg_geom.vertexAt(n_coords - 1)

            crow_km = func.measureLine(
                QgsPointXY(start_pt), QgsPointXY(end_pt)) / 1000  # km
            river_length_km = func.measureLength(seg_geom) / 1000  # km

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


            attrs = [
                seg_idx + 1,
                str(river_id),
                round(crow_km, 4),
                round(river_length_km, 4),
                round(sinuosity, 4),
            ]
            if len(attrs) != fields.count():
                raise QgsProcessingException(
                    f"Attribute mismatch writing segment {seg_idx + 1}: "
                    f"{len(attrs)} values for {fields.count()} fields.")

            out_feature = QgsFeature(fields)
            out_feature.setGeometry(seg_geom)
            out_feature.setAttributes(attrs)
            if not sink.addFeature(out_feature, QgsFeatureSink.FastInsert):
                raise QgsProcessingException(
                    f"Failed to add feature for segment {seg_idx + 1}.")

            feedback.setProgress(int((seg_idx + 1) / total_segments * 100))

        feedback.pushInfo(
            f"=== Complete: {total_segments} segments processed ===")
        return {self.OUTPUT: dest_id}