/* Synthetic contract fixtures for browser boundary tests, not a native fire calculation. */
const FIRST = "11111111-1111-4111-8111-111111111111";
const SECOND = "22222222-2222-4222-8222-222222222222";
const JOB = "33333333-3333-4333-8333-333333333333";
const GEOMETRY = {type:"Polygon",coordinates:[[
  [-121,39],[-120.999,39],[-120.999,39.001],[-121,39.001],[-121,39],
]]};
function landscape(overrides={}) {
  return {schema_version:"2",assessment_id:FIRST,fire_ref:"esri-observation-1",input_hash:"a".repeat(64),
    model_version:"landscape-surface-fire-1.0.0",availability:"available",mode:"landscape",
    calculated_at:"2026-10-27T12:00:00Z",fire:{latitude:39,longitude:-121,name:"Fixture fire",
      source_id:"esri-1",source_timestamp:"2026-10-27T11:00:00Z"},
    weather:{query_latitude:39,query_longitude:-121,source:"DTN",availability:"available",
      observed_at:"2026-10-27T11:55:00Z",wind_speed_mps:0,wind_from_degrees:null},
    scenario:{engine:"Pyrothermel/Behave directional arrival grid",moisture_profile:"moderate",
      wind_height_assumption:"DTN wind treated as 10 m above ground; open unsheltered fuel assumed",
      initialization:"reported point treated as demo ignition",horizons_minutes:[30,60],directional_radius:3},
    landscape:{manifest:{region:"CONUS",fuel_product:"LANDFIRE LF2024 FBFM40",elevation_product:"LANDFIRE LF2020 Elev",
      fuel_source:"https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2024/LF2024_FBFM40_CONUS/ImageServer",
      elevation_source:"https://lfps.usgs.gov/arcgis/rest/services/Landfire_Topo/LF2020_Elev_CONUS/ImageServer",
      captured_at:"2026-10-27T11:58:00Z",crs:"EPSG:5070",width:101,height:101,resolution_m:30,
      west:0,north:3030,nodata:-9999,elevation_units:"metres",
      raster_sha256:"a".repeat(64),fuel_sha256:"b".repeat(64),elevation_sha256:"c".repeat(64)},
      solver:"directed-cell-arrival-1",directional_radius:3,ignition_row:50,ignition_column:50,
      ignition_fuel_model:101,ignition_slope_degrees:15,ignition_aspect_degrees:90,
      reached_cells:1,spread_cells:0,modeled_cells:9000,nonburnable_cells:1100,nodata_cells:101,
      native_states:1,maximum_arrival_minutes:60},
    geojson:{type:"FeatureCollection",features:[30,60].map(elapsed_minutes => ({type:"Feature",geometry:GEOMETRY,
      properties:{kind:"landscape",label:"Synthetic retained terrain fixture",elapsed_minutes}}))},
    assumptions:["Synthetic UI contract fixture, not a native result."],warnings:[],failure:null,...overrides};
}
const reference = {job_id:JOB,fire_refs:["esri-observation-1"]};
const locations = [{latitude:39,longitude:-121,fire:{name:"Fixture fire",sourceId:"esri-1"}}];
const response = (status, overrides={}) => ({...reference,status,
  phase:status==="completed"?"Assessment retained":"Preparing landscape",progress:status==="completed"?1:.25,
  assessments:status==="completed"?[landscape()]:[],error:null,...overrides});
module.exports = {FIRST,SECOND,JOB,GEOMETRY,landscape,reference,locations,response};
