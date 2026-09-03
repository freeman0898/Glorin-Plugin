#Glorin Sinuosity Plugin

This plugin computes river sinuosity and environmental indices (NDVI, NDWI, MNDWI)
for a manually selected river section using Google Earth Engine satellite imagery.

#Features
The user has to select a river line layer, a CRS (preferably a UTM), and a start and 
end point for a section of river (can cover multiple rivers so long as they feed into 
one another and overlap in the shapefile). The plugin then calculates straight line
distance, river length and sinuosity before pulling Sentinel-2 or Landsat imagery 
based on a buffer of that section of river and computes statistics that the user can
select before writing to a single output feature.

#Requirements
This plugin works on QGIS version 3.0 and newer. It also requires a Google account with
Earth Engine access and a registered GEE Cloud project. ***Write more on this after testing
It is also important to note that when first launching, the login menu/pop-up may load in 
being other open applications, so users may need to click on the QGIS icon in their taskbar
to force the menu forward.

#Known Limitations
****

#Acknowledgement
This plugin is based on the QGIS Earth Engine Plugin (MIT License, © 2019 Gennadii Donchyts) 
and uses copied or modified versions of the GEE plugin code for features relating to the 
connection and use of GEE within this plugin.

This plugin is built for the Warisin project and is designed to use the shapefiles from the 
GLORIN (Global LOng and Large Rivers INventory) dataset.