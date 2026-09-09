# Native S-57 research fixtures

The files under `noaa/` are unchanged binary NOAA ENC exchange sets retrieved on
2026-10-04. The `.000`, `.001` and `.002` files beside each ZIP are exact copies
of its corresponding `ENC_ROOT/<cell>/` members. They are not generated GeoJSON.

| Cell | Official download | Base / supplied updates |
|---|---|---|
| US5A1KMJ | [NOAA US5A1KMJ.zip](https://www.charts.noaa.gov/ENCs/US5A1KMJ.zip) | Edition 1 / 0, 1, 2 |
| US5A1KMK | [NOAA US5A1KMK.zip](https://www.charts.noaa.gov/ENCs/US5A1KMK.zip) | Edition 2 / 0, 1, 2 |

The [NOAA ENC dataset record](https://repository.library.noaa.gov/view/noaa/71555)
identifies CC0-1.0. Also retain the [NOAA redistribution agreement](https://www.charts.noaa.gov/ENCs/ENC_Agreement.shtml):
the original ZIPs contain `USERAGREEMENT.TXT`, `README.TXT`, the exchange catalog,
and ancillary text. Redistributed fixtures are not an official NOAA ENC product
for navigation or regulatory carriage. No NOAA endorsement is implied.

These frozen research samples must not be used for navigation. Their retrieval
date is not a chart currency guarantee. Attribution, byte counts, SHA256 values,
official technical sources, and known coverage limits are recorded in
`resources/research/s57_sources.json`.

The independent tests decode the actual ISO8211 sounding integer arrays and
spatial pointers to check coordinate scaling, sign, and a polygon hole. Malicious
archives and altered metadata are created in memory and explicitly distinguished
from the original NOAA bytes. No IHO S64 chart data are included: a GDAL test
script's MIT license does not establish the external chart's data license.
