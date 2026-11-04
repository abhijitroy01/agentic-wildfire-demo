/* Compact saved-scenario controls retain provenance and expose only real returned time steps. */
const test = require("node:test");
const assert = require("node:assert/strict");
const React = require("react");
const {act,create} = require("react-test-renderer");
const {load} = require("../ts-module.cjs");
const {FIRST,landscape,reference,response} = require("./fire-risk-fixtures.cjs");

const Summary = () => load("features/fire-exposure/components/FireExposureSummary.tsx",{stubs:{
  "./FireExposureSummary.css":{},"./Accordion.css":{},
}}).default;
const text = node => typeof node === "string" || typeof node === "number" ? String(node)
  : (node.children ?? []).map(text).join("");
const completed = result => [{reference,result:response("completed",{assessments:[result]}),readError:null}];

test("the compact scenario retains essential evidence while full provenance starts in a closed disclosure",async()=>{
  const result=landscape({assumptions:["A complete retained scientific assumption."],
    warnings:["A retained model warning."]});
  const original=JSON.stringify(result),previousFetch=global.fetch;
  global.fetch=()=>{throw new Error("Evidence disclosure cannot call a provider or model");};
  let renderer;
  try{
    await act(async()=>{renderer=create(React.createElement(Summary(),{assessments:[result],selections:{},
      jobs:completed(result),onScenarioTime:()=>undefined}));});
    const section=renderer.root.findByType("section");
    const details=section.findByType("details");
    assert.equal(details.props.open,undefined,"Native details starts closed without hiding the source evidence");
    assert.equal(text(details.findByType("summary")),"Model details");
    assert.match(text(details),/A complete retained scientific assumption/);
    assert.match(text(details),/A retained model warning/);
    assert.match(text(details),/LANDFIRE LF2024 FBFM40/);
    assert.match(text(details),/LANDFIRE LF2020 Elev/);
    assert.match(text(details),/esri-1/);
    assert.match(text(details),/not evacuation zones, burn probabilities or operational forecasts/);
    assert.match(text(section.findAllByType("p").find(node=>node.props.className==="fire-exposure-resolution")),
      /30 m model cells/);
    assert.match(text(section),/Modeled spread from an assumed point ignition/);
    assert.match(text(section),/DTN measured calm wind/);
    assert.match(text(section.findByProps({className:"fire-exposure-observations"})),/Fire record updated.*Weather observed/);
    assert.equal(JSON.stringify(result),original,"Disclosure does not mutate retained model evidence");
  }finally{
    if(renderer)await act(async()=>renderer.unmount());
    global.fetch=previousFetch;
  }
});

test("identical real horizons explain no additional growth without inventing a larger shape or extra time",async()=>{
  const base=landscape(),result={...base,landscape:{...base.landscape,spread_cells:2,reached_cells:3}};
  const original=JSON.stringify(result),changes=[],previousFetch=global.fetch;
  global.fetch=()=>{throw new Error("Changing saved geometry cannot start model work");};
  let renderer;
  const props={assessments:[result],selections:{},jobs:completed(result),
    onScenarioTime:(...args)=>changes.push(args)};
  try{
    await act(async()=>{renderer=create(React.createElement(Summary(),props));});
    const slider=renderer.root.findAllByType("input").find(node=>node.props.type==="range");
    assert.equal(slider.props.max,1,"Only the two actual returned horizons are selectable");
    assert.equal(slider.props["aria-valuetext"],"60 minutes after assumed initialization");
    assert.match(text(renderer.root),/After assumed ignition/);
    assert.match(text(renderer.root),/No additional modeled growth since the previous time step/);
    await act(async()=>slider.props.onChange({currentTarget:{value:"0"}}));
    assert.deepEqual(changes,[[FIRST,30]]);
    await act(async()=>renderer.update(React.createElement(Summary(),{...props,selections:{[FIRST]:30}})));
    assert.doesNotMatch(text(renderer.root),/No additional modeled growth since the previous time step/,
      "The first returned horizon has no preceding comparison");
    await act(async()=>renderer.update(React.createElement(Summary(),{...props,selections:{[FIRST]:null}})));
    assert.equal(renderer.root.findAllByType("input").find(node=>node.props.type==="range").props.disabled,true);
    assert.doesNotMatch(text(renderer.root),/No additional modeled growth since the previous time step/);
    assert.equal(JSON.stringify(result),original,"No-growth explanation preserves every returned coordinate");
  }finally{
    if(renderer)await act(async()=>renderer.unmount());
    global.fetch=previousFetch;
  }
});

test("zero spread and input uncertainty stay visible while details are closed",async()=>{
  const result=landscape();
  let renderer;
  try{
    await act(async()=>{renderer=create(React.createElement(Summary(),{assessments:[result],selections:{},
      jobs:completed(result),onScenarioTime:()=>undefined}));});
    const directText=renderer.root.findByType("section").children.filter(node=>node.type==="p").map(text).join(" ");
    assert.match(directText,/No modeled growth beyond the ignition cell/);
    assert.match(directText,/101 cells have missing inputs; they are not established as safe/);
    assert.doesNotMatch(text(renderer.root),/No additional modeled growth since the previous time step/,
      "The specific ignition-cell outcome avoids duplicate no-growth prose");
  }finally{if(renderer)await act(async()=>renderer.unmount());}
});

test("a failed assessment retains its failure and fire handoff without fabricated controls or model resolution",async()=>{
  const result=landscape({availability:"unavailable",landscape:null,
    failure:{code:"LANDSCAPE_REQUEST_FAILED",message:"Landscape source request failed."},
    geojson:{type:"FeatureCollection",features:[]}});
  const actions=[];
  let renderer;
  try{
    await act(async()=>{renderer=create(React.createElement(Summary(),{assessments:[result],selections:{},
      jobs:completed(result),onScenarioTime:()=>undefined,onSimulateReportedFire:id=>actions.push(id)}));});
    assert.match(text(renderer.root),/Landscape source request failed/);
    assert.match(text(renderer.root),/LANDSCAPE_REQUEST_FAILED/);
    assert.equal(renderer.root.findAllByType("input").length,0);
    assert.doesNotMatch(text(renderer.root),/30 m model cells/);
    const action=renderer.root.findAllByType("button").find(node=>text(node)==="Plan evacuation for this fire");
    await act(async()=>action.props.onClick());
    assert.deepEqual(actions,[FIRST],"The explicit handoff retains the existing immutable assessment ID");
  }finally{if(renderer)await act(async()=>renderer.unmount());}
});
