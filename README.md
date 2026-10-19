#Glorin Sinuosity Plugin

This plugin computes river sinuosity and environmental indices (NDVI, NDWI, MNDWI, etc.)
for a manually selected river section using Google Earth Engine or locally 
available satellite imagery..

#Features
There are both offline and online tools within this plugin. Offline tools allow the 
user to determine sinuosity of a river either as a whole, by distance or numbered 
segments or with manually selected endpoints. There is also a local custom calculator
for the user to input local satellite imagery, input any band math to calculate, and
compare sinuosity of their selection of river with the surrounding environmental index
of choice. Online tools also find the sinuosity based on the user selection above, but 
call on the Google Earth Engine to calculate the indices selected (or input for the custom
calculator) and will return a table of the calculated indices/functions.

#Requirements
This plugin works on QGIS version 3.0 and newer. It also requires a Google account with
Earth Engine access and a registered GEE Cloud project.
It is recommended that users sign into their GEE accounts upon download since it is the first 
step the plugin goes through upon download. This step is not required again unless changing
GEE account. After the account is verified, the plugin can be used both online and offline.

#Known Limitations
****

#Acknowledgement
This plugin is based on the QGIS Earth Engine Plugin (MIT License, © 2019 Gennadii Donchyts) 
and uses copied or modified versions of the GEE plugin code for features relating to the 
connection and use of GEE within this plugin.

This plugin is built for the Warisin project and is designed to use the shapefiles from the 
GLORIN (Global LOng and Large Rivers INventory) dataset.

This plugin was aided with the use of GreenPT AI to help troubleshoot code errors
during development. All code has been reviewed and tested by the author.