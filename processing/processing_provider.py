# Portions of this file are derived from the QGIS Earth Engine Plugin
# (https://github.com/gee-community/qgis-earthengine-plugin),
# Copyright (c) 2019 Gennadii Donchyts, licensed under the MIT License.


from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon


from .sinuosity import SinuosityAlgorithm
from .River_analysis import RiverAnalysisAlgorithm
from .segmented_analysis import SegmentedRiverAnalysisAlgorithm
 
class GlorinProcessingProvider(QgsProcessingProvider):
    """Provider for GLORIN river analysis tools."""
    def __init__(self, icon: QIcon, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._icon = icon
    
    def id(self) -> str:
        return "glorin"
 
    def name(self) -> str:
        return "GLORIN"
 
    def loadAlgorithms(self):
        """
        Register every algorithm this plugin exposes. To add a new tool,
        import it at the top of this file and add one line here.
        """
        self.addAlgorithm(SinuosityAlgorithm())
        self.addAlgorithm(RiverAnalysisAlgorithm())
        self.addAlgorithm(SegmentedRiverAnalysisAlgorithm())
        
    def icon(self) -> QIcon:
        return self._icon
