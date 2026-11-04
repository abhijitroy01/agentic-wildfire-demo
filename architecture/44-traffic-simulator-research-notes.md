# Simulator research and source register

This register preserves the incident evidence, model references and provider limitations used to choose the simulator. The research was performed September 16, 2026; descriptions of access tested then are historical, not current provider entitlement. Current behavior is in [simulator architecture](32-traffic-simulator-architecture.md), [business justification](33-evacuation-crew-access-business-justification.md) and [known limits](00-current-system.md#known-limits-and-backlog).

## Decisions that matter when extending the model

Use a connected directed road network rather than treating a single highway line as a complete evacuation system. Account for civilian departure delay and crew dispatch waiting, not only road travel time. Include residents who never leave and crews who never arrive. Disable automatic teleporting as a substitute for successful evacuation and preserve blocked-origin demand. Confirm the actual engine configuration and regression fixtures before changing policy semantics.

SUMO is the implemented traffic engine. A circular expanding hazard and synthetic demand are explicit assumptions. WUI-NITY and coupled fire/traffic research provide validation references; they do not certify this implementation. NIST Camp Fire/ESCAPE material motivates joint resident and responder outcomes and the distinction between leaving the model and reaching safety.

TomTom speed observations do not establish measured demand. PeMS access/ingestion is unimplemented. Public fire detections/perimeters are contextual and do not validate an assumed incident or forecast. Source timing, geometry coverage, unmatched roads, attribution and provider retention/use terms must remain visible. OSM data licensing and community tile-server terms are separate. Caltrans CCTV stream limits/agreements also differ from metadata access.

## Traffic-data providers

TomTom flow segments are the current-traffic source (D45). The segment API documents an HTTP response `Date` but no per-measurement timestamp, so retrieval freshness must never be shown as a sensor observation time. A free request quota does not establish coverage, model calibration or permission to keep provider data; TomTom's storage, export and use terms are not yet confirmed, so confirm them before keeping provider data indefinitely with immutable recordings. Caltrans PeMS registration is free, but a website account does not grant an automated real-time feed. 511 SF Bay adds Bay Area events and closures only; Waze for Cities is limited to approved partners; the Google Maps traffic layer is a visualization, not a numeric feed for SUMO.

- [TomTom pricing and free-tier FAQ](https://docs.tomtom.com/pricing), [Flow Segment Data fields](https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/traffic-flow/flow-segment-data), [market coverage](https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/product-information/market-coverage) (United States supported; individual-road coverage not guaranteed)
- [Caltrans PeMS data source](https://dot.ca.gov/programs/traffic-operations/mpr/pems-source), [PeMS account application](https://pems.dot.ca.gov/?dnode=apply), [CWWP2 travel times](https://cwwp2.dot.ca.gov/travel-times.html)
- [511 SF Bay traffic APIs](https://511.org/open-data/traffic), [Waze for Cities](https://www.waze.com/wazeforcities), [Google Maps traffic layer](https://developers.google.com/maps/documentation/javascript/trafficlayer)

## Source register


Sources below were accessed or inspected on September 16, 2026. “Documentation reviewed” means behavior described by the provider, not a tested account entitlement or service-level guarantee.

- **[R1] NIST Camp Fire life safety / TN 2252.** [Study overview](https://www.nist.gov/programs-projects/wildland-urban-interface-wui-fire-data-collection-parcel-vulnerabilities/nist/life), [full technical report](https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.2252.pdf). Full PDF extracted; relevant planning, traffic, map and photograph pages reviewed.
- **[R2] NIST ESCAPE TN 2262r1, March 2025.** [Updated guidance](https://www.nist.gov/news-events/news/2025/04/nist-updates-critical-wildfire-evacuation-and-sheltering-guidance), [full report](https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.2262r1.pdf). Relevant uncertainty and planning sections reviewed.
- **[R3] Caltrans State Highway Network Lines.** [Official layer and schema](https://caltrans-gis.dot.ca.gov/arcgis/rest/services/CHhighway/SHN_Lines/FeatureServer/0). Metadata and two geographic queries tested.
- **[R4] OSMnx and SUMO network import.** [OSMnx documentation](https://osmnx.readthedocs.io/en/stable/), [OSM import into SUMO](https://sumo.dlr.de/docs/Networks/Import/OpenStreetMap.html). Documentation reviewed.
- **[R5] SUMO Emergency Vehicle Simulation.** [Emergency behavior](https://sumo.dlr.de/docs/Simulation/Emergency.html). Defaults and blue-light behavior reviewed.
- **[R6] SUMO teleport behavior.** [Why vehicles are teleporting](https://sumo.dlr.de/docs/Simulation/Why_Vehicles_are_teleporting.html). Documentation reviewed.
- **[R7] SUMO calibrators.** [Calibrator documentation](https://sumo.dlr.de/docs/Simulation/Calibrator.html). Vehicle insertion/removal and jam handling reviewed.
- **[R8] SUMO runtime control.** [TraCI](https://sumo.dlr.de/docs/TraCI/index.html). Documentation reviewed; engine not installed or benchmarked in this research.
- **[R9] Caltrans LCS.** [Feed documentation](https://cwwp2.dot.ca.gov/lane-closure.html), [field definitions](https://cwwp2.dot.ca.gov/documentation/lcs/lcs-field-description.html), [tested District 3 JSON](https://cwwp2.dot.ca.gov/data/d3/lcs/lcsStatusD03.json).
- **[R10] Caltrans CCTV.** [Feed documentation and fair-use terms](https://cwwp2.dot.ca.gov/closed-circuit-television-cameras.html), [tested District 3 metadata](https://cwwp2.dot.ca.gov/data/d3/cctv/cctvStatusD03.json). No streams tested.
- **[R11] Caltrans travel times and PeMS.** [TT documentation](https://cwwp2.dot.ca.gov/travel-times.html), [tested District 3 TT response](https://cwwp2.dot.ca.gov/data/d3/tt/ttStatusD03.json), [PeMS](https://pems.dot.ca.gov/). PeMS public description only; authenticated data access not tested.
- **[R12] NIFC WFIGS Current Interagency Fire Perimeters.** [Authoritative item](https://www.arcgis.com/home/item.html?id=d1c32af3212341869b3c810f1a215824), [service](https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer). Metadata, timing and inclusion rules reviewed; current fire features were not used to declare an incident.
- **[R13] NASA FIRMS.** [Area API](https://firms.modaps.eosdis.nasa.gov/api/area/), [FAQ](https://www.earthdata.nasa.gov/data/tools/firms/faq). API contract, latency and detection limitations reviewed; no MAP_KEY issued or used.
- **[R14] NWS API.** [Documentation](https://www.weather.gov/documentation/services-web-api). API, point lookup and observation-delay documentation reviewed.
- **[R15] TomTom Traffic API.** [Flow Segment Data](https://developer.tomtom.com/traffic-api/documentation/tomtom-maps/v1/traffic-flow/flow-segment-data). Documented fields reviewed; no authenticated live requests.
- **[R16] HERE Traffic API v7.** [Developer guide](https://www.here.com/docs/bundle/traffic-api-developer-guide-v7/page/README.html). Flow/incident/geographic capabilities reviewed; no authenticated live requests.
- **[R17] WUI-NITY.** [2021 paper](https://doi.org/10.1016/j.ssci.2020.105145), [WUI-NITY 4 full report, October 2024](https://content.nfpa.org/-/media/Project/Storefront/Catalog/Files/Research/Research-Foundation/Reports/WUI/RFWUI-NITY4.pdf). Earlier abstract and 2024 report reviewed, especially pp. 117–120 and 136.
- **[R18] Li, Cova and Dennison (2018).** [Setting Wildfire Evacuation Triggers by Coupling Fire and Traffic Simulation Models](https://doi.org/10.1007/s10694-018-0771-6). Public abstract and references reviewed; subscription full text not accessed.
- **[R19] Chen, Shafi and Chen (2020).** [Simulation pipeline for traffic evacuation in urban areas and emergency traffic management policy improvements through case studies](https://doi.org/10.1016/j.trip.2020.100210). Abstract retrieved through scholarly metadata and cross-checked against the verification paper's reference list; full experiment not reproduced.
- **[R20] Ronchi et al. (2023).** [The verification of wildland–urban interface fire evacuation models](https://doi.org/10.1007/s11069-023-05913-2). Open full text reviewed.
- **[R21] Gwynne et al. (2023).** [Roxborough Park Community Wildfire Evacuation Drill: Data Collection and Model Benchmarking](https://doi.org/10.1007/s10694-023-01371-1). Open full text reviewed.
- **[R22] Daganzo (1994).** [The cell transmission model: A dynamic representation of highway traffic consistent with the hydrodynamic theory](https://doi.org/10.1016/0191-2615(94)90002-7). Bibliographic identity verified; original full text not retrieved.
- **[R23] Visualization.** [MapLibre GL JS](https://maplibre.org/maplibre-gl-js/docs/), [deck.gl TripsLayer](https://deck.gl/docs/api-reference/geo-layers/trips-layer). Documentation reviewed.
- **[R24] OpenStreetMap licensing.** [Copyright and license](https://www.openstreetmap.org/copyright). Attribution and data-license information reviewed.
- **[R25] Fire-model inputs.** [USFS FlamMap](https://research.fs.usda.gov/firelab/products/dataandtools/flammap), [LANDFIRE](https://landfire.gov/). Desktop platform, FARSITE inclusion and required landscape layers reviewed.
- **[R26] Additional source context.** [Caltrans RWIS](https://cwwp2.dot.ca.gov/roadway-weather-information-systems.html), [RWIS fields](https://cwwp2.dot.ca.gov/documentation/rwis/rwis-field-description.html), [CMS](https://cwwp2.dot.ca.gov/changeable-message-signs.html), [CHP incidents](https://cad.chp.ca.gov/Traffic.aspx), [ACS five-year data](https://www.census.gov/data/developers/data-sets/acs-5year.html). Public documentation/metadata reviewed.

Additional useful empirical background: Wong, Broader and Shaheen, [Review of California Wildfire Evacuations from 2017 to 2019](https://doi.org/10.7922/G29G5K2R); Zehra et al., [Systematic review and research gaps on wildfire evacuations](https://doi.org/10.1080/03081060.2024.2348713). Abstracts reviewed through scholarly metadata; full texts not reviewed here.
