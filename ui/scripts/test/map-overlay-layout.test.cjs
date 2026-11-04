/* Native browser overlay checks include map context, saved evidence and the real scale widget. */
const test=require("node:test");
const assert=require("node:assert/strict");
const {mapOverlayLayout}=require("../browser/helpers.cjs");

const rectangle=(left,top,width,height)=>({left,top,right:left+width,bottom:top+height,width,height});
const required=[".fire-exposure-summary",".map-legend",".map-context-controls",".esri-scale-bar",".map-undock-control"];
const map=(boxes)=>({evaluate:(check,argument)=>check({
  getBoundingClientRect:()=>rectangle(0,0,390,575),
  querySelector:selector=>boxes[selector] ? {getBoundingClientRect:()=>boxes[selector]} : null,
},argument)});
const disjoint=()=>Object.fromEntries(required.map((selector,index)=>[selector,rectangle(index*70,100,60,40)]));

test("phone overlay checks include saved evidence, native scale, context and undock even without wind",async()=>{
  assert.deepEqual(await mapOverlayLayout(map(disjoint())),{missing:[],overlaps:[],outside:[]});
  const boxes=disjoint();delete boxes[".esri-scale-bar"];
  assert.deepEqual((await mapOverlayLayout(map(boxes))).missing,[".esri-scale-bar"],
    "A hidden or missing native scale cannot pass legend acceptance");
});

test("wind is optional without observations but is included when rendered, even when explicitly required",async()=>{
  const boxes=disjoint();
  boxes[".wind-legend"]=rectangle(0,200,100,40);
  boxes[".response-units-legend"]=rectangle(200,200,100,40);
  assert.deepEqual(await mapOverlayLayout(map(boxes),[".response-units-legend",".wind-legend"]),
    {missing:[],overlaps:[],outside:[]},"One rendered wind legend is checked once");
  boxes[".wind-legend"]=rectangle(0,100,100,40);
  assert.ok((await mapOverlayLayout(map(boxes))).overlaps.includes(".fire-exposure-summary × .wind-legend"));
});

test("native scale covered by a legend fails and nonintersecting touching edges pass",async()=>{
  const boxes=disjoint();
  boxes[".esri-scale-bar"]=rectangle(70,100,60,40);
  assert.ok((await mapOverlayLayout(map(boxes))).overlaps.includes(".map-legend × .esri-scale-bar"));
  boxes[".esri-scale-bar"]=rectangle(70,140,60,40);
  assert.deepEqual((await mapOverlayLayout(map(boxes))).overlaps,[]);
});

test("controls outside the map and zero-sized cards cannot pass acceptance",async()=>{
  const boxes=disjoint();
  boxes[".map-context-controls"]=rectangle(320,10,80,40);
  boxes[".map-undock-control"]=rectangle(300,10,0,40);
  assert.deepEqual(await mapOverlayLayout(map(boxes)),{
    missing:[".map-undock-control"],overlaps:[],outside:[".map-context-controls"],
  });
});
