"""
***************************************************************************
*                                                                         *
*   This program is free software; you can redistribute it and/or modify  *
*   it under the terms of the GNU General Public License as published by  *
*   the Free Software Foundation; either version 2 of the License, or     *
*   (at your option) any later version.                                   *
*                                                                         *
***************************************************************************
"""

from typing import Any, Optional

from qgis.core import (
    QgsFeatureSink,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterPoint,
    QgsDistanceArea,
    QgsProcessingParameterCrs,
    QgsCoordinateTransform,
    QgsProject,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsWkbTypes,
    )
    
from PyQt5.QtCore import QVariant

from qgis import processing


class SinuosityAlgorithm(QgsProcessingAlgorithm):
    """
    This is a script to calculate the sinuosity of a river shapefile over all 
    rivers in that shapefile
    """

    INPUT = "INPUT"
    OUTPUT = "OUTPUT"
    START_POINT = 'START POINT'
    END_POINT = 'END POINT'
    
    def name(self) -> str:
        return "Sinuosity"

    def displayName(self) -> str:
        return "Sinuosity Calculation"

    def group(self) -> str:
        return "GLORIN"

    def groupId(self) -> str:
        return "glorin"

    def shortHelpString(self) -> str:
        return "This tool can be used to find only the sinuosity of a river from a shapefile."
        "Also calculates sinuosity over multiple rivers so long as they flow into each other. "

    def initAlgorithm(self, config: Optional[dict[str, Any]] = None):

        self.addParameter(QgsProcessingParameterFeatureSource(
        'INPUT',
        'Input layer'))
        self.addParameter(QgsProcessingParameterCrs(
        'CRS',
        'Input CRS'))
        self.addParameter(QgsProcessingParameterPoint(
        'START POINT',
        'Start point',))
        self.addParameter(QgsProcessingParameterPoint(
        'END POINT',
        'End point',))
        self.addParameter(QgsProcessingParameterFeatureSink(
        'OUTPUT',
        'Sinuosity Results'))


    def processAlgorithm(self, parameters, context, feedback):
        
        river_layer = self.parameterAsLayer(parameters, 'INPUT', context)
        start_point = self.parameterAsPoint(parameters, 'START POINT', context)
        end_point = self.parameterAsPoint(parameters, 'END POINT', context)
        crs= self.parameterAsCrs(parameters, 'CRS', context)
        
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
            context.transformContext())
            
        start_point = transform.transform(start_point)
        end_point = transform.transform(end_point)
        
        func = QgsDistanceArea()
        func.setSourceCrs(crs, context.transformContext())
        func.setEllipsoid(crs.ellipsoidAcronym())

        crow = func.measureLine(start_point, end_point)/1000
        
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
        
        path_layer = context.getMapLayer(path_result['OUTPUT'])
        
        if path_layer is None or path_layer.featureCount() == 0:
            raise QgsProcessingException(
            "No path found between the points. "
            "Check that the points are near the river network "
            "and that the lines are connected.")
            
        
        river_length = 0.0
        for feature in path_layer.getFeatures():
            geom = feature.geometry()
            river_length += func.measureLength(geom)/1000
            
            
        sinuosity = river_length/crow
        
        feedback.pushInfo(f"Crow-flies distance: {crow} km")
        feedback.pushInfo(f"River length: {river_length} km")
        feedback.pushInfo(f"Sinuosity: {sinuosity}")
        
        fields=QgsFields()
        fields.append(QgsField('crow_flies', QVariant.Double))
        fields.append(QgsField('river_length', QVariant.Double))
        fields.append(QgsField('sinuosity', QVariant.Double))
        
        (sink,dest_id) = self.parameterAsSink(
            parameters,
            'OUTPUT',
            context,
            fields,
            QgsWkbTypes.NoGeometry,
            crs)
        
        out_feature = QgsFeature(fields)
        out_feature.setAttributes([crow, river_length, sinuosity])
        sink.addFeature(out_feature, QgsFeatureSink.FastInsert)
        
        return{'OUTPUT': dest_id}


    def createInstance(self):
        return self.__class__()