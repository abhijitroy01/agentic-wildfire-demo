# Third-party notices

This project's own source is licensed under the [Apache License 2.0](LICENSE). The software, data and services
below belong to third parties and keep their own terms; this project's license does not cover them. Product and
company names identify those offerings only; their owners do not endorse this project.

## Software installed by package managers and container builds

The repository vendors none of these packages. `requirements-lock.txt`, `ui/package-lock.json` and
`deployment/infra/package-lock.json` pin them, and the Dockerfiles install or compile them into the images. Most are
under MIT, BSD, ISC or Apache-2.0 terms. These need attention when you redistribute a UI build or a container image:

| Component | Used by | License |
|---|---|---|
| ArcGIS Maps SDK for JavaScript (`@arcgis/core`, `@arcgis/lumina`, `@arcgis/toolkit`) and Calcite (`@esri/calcite-components`, `@esri/calcite-ui-icons`) | UI maps | Proprietary, under the Esri Master License Agreement. Redistribution is allowed only unmodified, with Esri's copyright notice; see `ui/node_modules/@arcgis/core/copyright.txt` after `npm install`. |
| Eclipse SUMO 1.27.1 (`eclipse-sumo`, `sumolib`, `traci`, `sumo-data`; compiled from source in `traffic-simulator/Dockerfile`) | Traffic simulation | EPL-2.0 OR GPL-2.0-or-later |
| Osmium Tool, libosmium and protozero (compiled in `traffic-simulator/Dockerfile`) | OpenStreetMap extraction | GPL-3.0-or-later (Osmium Tool), BSL-1.0 (libosmium), BSD-2-Clause (protozero) |
| Xerces-C 3.3.0, PROJ 9.9.0 and Boost.Program_options (SUMO build) | Traffic simulation | Apache-2.0, MIT and BSL-1.0 |
| FFmpeg 9.0.2 (compiled in `broadcastify/monitor/Dockerfile` as an LGPL build) | Radio audio decoding | LGPL-2.1-or-later |
| Pyrothermel 0.1.4 (built from its PyPI source in `fire-risk-modeler/Dockerfile`) | Surface fire calculation | MIT; see the package for the terms of the Behave source it builds |
| certifi | TLS certificate bundle | MPL-2.0 |
| axe-core (UI tests only) | Accessibility checks | MPL-2.0 |

The traffic simulator and radio images copy these components' license files into `/opt/sumo/share/licenses/` and
`/opt/ffmpeg/share/licenses/`. Every image also contains the Debian base system of `python:3.14-slim-trixie` and its
packages under their own licenses.

## Data included in this repository

| Data | Source and terms |
|---|---|
| `backend/knowledge/public-sop/` (guidance text and page snapshots) | FEMA (Ready.gov), National Park Service and USDA Forest Service web pages. Works of the U.S. federal government are not subject to U.S. copyright (17 U.S.C. § 105); FEMA states that most material on its site may be copied without permission and asks for a citation. `sources.json` records each source URL and retrieval date. |
| `traffic-simulator/traffic_simulator/data/*-highways.json` | Extracts of the California Department of Transportation (Caltrans) State Highway Network GIS layer. Caltrans publishes its GIS data without warranty; check its current terms before you redistribute these files. |
| `fire-risk-modeler/tests/data/king-landscape.json` | A LANDFIRE fuel and elevation crop (U.S. Geological Survey and USDA Forest Service). LANDFIRE data is in the public domain; cite LANDFIRE and note modifications. |
| Other test fixtures, including `ui/test-fixtures/browser-synthetic-grid/` | Written or generated for this project; synthetic values are labeled as such. |

## Services the application calls at runtime

The application downloads or queries these services when it runs. Each needs your own account or key where noted,
and use must follow the provider's terms:

- **OpenStreetMap** data through Overpass and Geofabrik extracts: Open Database License 1.0, with attribution
  "© OpenStreetMap contributors". Road networks built from it are derived databases under the same license. The UI
  map tiles come from `tile.openstreetmap.org`, which the OpenStreetMap Foundation's tile usage policy governs; a
  heavily used installation needs its own tile service.
- **Esri ArcGIS** hosted services, including the public wildfire incident feeds: Esri terms of use.
- **USGS** map and terrain services: public domain.
- **LANDFIRE** landscape data: public domain.
- **NASA FIRMS** fire detections: needs a free `MAP_KEY`; follow NASA's data use and citation guidance.
- **DTN** weather: commercial; needs a DTN license.
- **TomTom** Traffic API: commercial; needs a TomTom key.
- **Broadcastify** radio content: the radio service plays operator-supplied recordings. This repository contains no
  recordings or Broadcastify content; obtaining and using recordings is subject to Broadcastify's terms.
- **Delphire** camera API (optional): commercial; needs a Delphire key.
- **Amazon Web Services**, including Amazon Bedrock and its model providers' terms, Amazon Location Service and
  AgentCore: AWS Customer Agreement and AWS Service Terms.
