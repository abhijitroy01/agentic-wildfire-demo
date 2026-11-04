import type { Discovery,FocusRequest,Result,Scenario,Sources } from "@/features/evacuation/artifacts";
import { corridorId,corridorLabel } from "@/features/evacuation/artifacts";
/**
 * Traffic Lab: free-form SUMO corridor research (custom coordinates, crews, hazard,
 * traffic mode and seed), open to every visitor through the demo session; results and replay reuse
 * the Evacuation workspace components. The run lifecycle lives in useResearchJob.
 */
import {
BottleneckExplanation,clock,Comparison,CrewExplanation,crewsReturned,download,feet,FEET_PER_METER,
SimulationMap as MapPanel,MapGuide,MetricCards,miles,MILES_PER_KM,MissionComparison,MissionFindings,peakSample,
POLICY_COLORS,PolicyInsights,policyNames,ProgressExplanation,ProviderReadings,ReplayExplanation,ReplayStatus,
RunEvidence,SourcePreview,TrafficEvidence,UnitInput,type Frame,
} from "@/features/evacuation/presentation";
import type React from "react";
import { useEffect,useRef,useState } from "react";
import { useMapFullscreen } from "@/shared/hooks/useMapFullscreen";
import { useResearchJob } from "../hooks/useResearchJob";
import { useRoadDiscovery } from "../hooks/useRoadDiscovery";
import { research } from "../services/api";
import { SourceCoverage } from "./SourceCoverage";

type Vehicle = Frame["vehicles"][number];
/** Map element id, distinct from the Evacuation workspace's map, which stays mounted (hidden). */
const MAP_ID = "traffic-lab-replay-map";
const EMPTY: Discovery["geometry"] = { type: "FeatureCollection", features: [] };
const defaults: Scenario = {
  lat: 39.7596, lon: -121.6219, radius_km: 8, highway: "State 191",
  vehicles: 400, people_per_vehicle: 2, crews: 4, duration_minutes: 30, departure_minutes: 10,
  seed: 42, exit_direction: "south", hazard_radius_m: 150, hazard_growth_m_min: 10,
  hazard_start_minutes: 5, background_speed_factor: 1,
  traffic_mode: "synthetic",
  policies: ["baseline", "coordinated", "phased"],
};

/** Traffic Lab page: scenario editor, source coverage, run history and result replay. */
export default function TrafficLab({ visible = true }: {
  /** The Traffic Lab view is on screen; it stays mounted (hidden) while another view is active. */
  visible?: boolean;
}): React.ReactElement {
  const [scenario, setScenario] = useState(defaults);
  const [discovery, setDiscovery] = useState<Discovery | null>(null);
  const [sources, setSources] = useState<Sources | null>(null);
  const [busy, setBusy] = useState("");
  const [policyIndex, setPolicyIndex] = useState(0);
  const [frameIndex, setFrameIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(20);
  const [focus, setFocus] = useState("overview");
  const fullscreen = useMapFullscreen(visible && focus !== "data");
  const [editing, setEditing] = useState(true);
  const [showRoutes, setShowRoutes] = useState(true);
  const [showTrails, setShowTrails] = useState(true);
  const [showTraffic, setShowTraffic] = useState(true);
  const [showCongestion, setShowCongestion] = useState(true);
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);
  const root = useRef<HTMLElement>(null);
  const lab = useResearchJob({ visible, loadStart: () => loadExample("paradise"), onLoaded: showLoaded });
  const { access, config, job, history, result, network, running, error, setError } = lab;
  const current = result?.policies[policyIndex];
  const frame = current?.frames[frameIndex];
  const liveMetrics = current?.series[frameIndex];
  const area = (): Pick<Scenario, "lat" | "lon" | "radius_km"> => ({ lat: scenario.lat, lon: scenario.lon, radius_km: scenario.radius_km });
  const change = (key: string, value: unknown): void => setScenario(s => ({ ...s, [key]: value }));
  const roadDiscovery = useRoadDiscovery(area(), discovered => {
    setDiscovery(discovered);
    change("highway", discovered.highways.length ? corridorId(discovered.highways[0]) : "");
  });
  const selectedCorridor = discovery?.highways.some(corridor => corridorId(corridor) === scenario.highway) ?? false;

  /** Show a completed run: its saved inputs, highway and sources, with the replay at the start. */
  function showLoaded(r: Result): void {
    roadDiscovery.clear();
    setScenario({ ...defaults, ...r.scenario });
    setDiscovery({ geometry: EMPTY, highways: [r.network.selected_route],
      source_mode: "Road corridor saved with this completed run" });
    setSources(r.sources); setPolicyIndex(0); setFrameIndex(0); setPlaying(false);
    setEditing(false); setFocus("overview"); setFocusRequest(null);
  }
  function clearReplay(): void {
    lab.clear(); setSources(null); setPlaying(false); setFrameIndex(0);
    setEditing(true); setFocus("overview"); setFocusRequest(null);
  }
  const moveArea = (key: string, value: number): void => {
    roadDiscovery.clear(); change(key, value); change("highway", ""); setDiscovery(null); clearReplay();
  };
  async function action(label: string, fn: () => Promise<void>): Promise<void> {
    setError(""); setBusy(label);
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(""); }
  }
  async function loadExample(name: string): Promise<void> {
    roadDiscovery.clear();
    await action("example", async () => {
      const data = await research<Discovery>(`/examples/${name}`);
      setScenario(s => ({ ...s, ...data.area, highway: name === "paradise" ? "State 191" : "State 1",
        exit_direction: name === "paradise" ? "south" : "east" }));
      setDiscovery(data); clearReplay();
    });
  }

  // A hidden or not-yet-connected lab keeps its state but stops the replay clock.
  useEffect(() => {
    if (!visible || access !== "ready") setPlaying(false);
  }, [visible, access]);

  useEffect(() => {
    if (!playing || !current) return;
    const interval = (current.frames[1]?.t || 5) * 1000 / speed;
    const timer = setInterval(() => setFrameIndex(i => {
      if (i >= current.frames.length - 1) { setPlaying(false); return i; }
      return i + 1;
    }), interval);
    return () => clearInterval(timer);
  }, [playing, current, speed]);

  function selectPolicy(i: number): void {
    const frames = result?.policies[i].frames;
    if (!frames?.length) return;
    const index = frames.findIndex(candidate => candidate.t >= (frame?.t ?? 0));
    setPolicyIndex(i); setFrameIndex(index < 0 ? frames.length - 1 : index); setPlaying(false);
  }
  async function inspectSources(): Promise<void> {
    setPlaying(false);
    if (result) setFocus("data");
    else if (!sources) await action("sources", async () => setSources(await research<Sources>("/sources", area())));
    requestAnimationFrame(() => root.current?.querySelector(result ? "#source-evidence" : "#observations")
      ?.scrollIntoView({ block: "start" }));
  }
  function inspectPolicy(i: number, seconds: number): void {
    const frames = result?.policies[i].frames;
    if (!frames) return;
    const index = frames.findIndex((f) => f.t >= seconds);
    setPolicyIndex(i); setFrameIndex(index < 0 ? frames.length - 1 : index); setPlaying(false);
    setFocus("bottlenecks");
    requestAnimationFrame(() => root.current?.querySelector(".scenario-toolbar")?.scrollIntoView({ block: "start" }));
  }
  function inspectFinding(policy: string, view: "overview" | "bottlenecks" | "crews"): void {
    const i = result?.policies.findIndex(p => p.policy === policy) ?? -1;
    if (i < 0 || !result) return;
    const target = result.policies[i];
    const seconds = view === "bottlenecks" && target.series.length
      ? peakSample(target.series).t : result.scenario.duration_minutes*60;
    inspectPolicy(i, seconds);
    setFocus(view);
  }
  function jumpTo(seconds: number): void {
    if (!current) return;
    const index = current?.frames.findIndex((f) => f.t >= seconds);
    setFrameIndex(index < 0 ? current.frames.length - 1 : index); setPlaying(false);
  }
  async function startRun(): Promise<void> {
    await action("run", async () => {
      await lab.start(scenario);
      setFrameIndex(0); setPlaying(false); setFocus("overview");
    });
  }
  const refreshSources = (): void => { void action("sources", async () => setSources(await research<Sources>("/sources", area()))); };
  const numberInput = (label: string, key: keyof typeof defaults, min: number, max: number, step = 1): React.ReactElement =>
    <label>{label}<input type="number" min={min} max={max} step={step} value={scenario[key] as number}
      onChange={e => change(key, e.target.value === "" ? "" : Number(e.target.value))}/></label>;

  const heading = <div className="evac-heading"><div><span className="eyebrow">RESEARCH SCENARIOS</span>
    <h2>Free-form experiments</h2><p>Choose a U.S. coordinate, road corridor, demand, crews and hazard.</p></div>
    {config && <span className="badge">{config.deployment} environment</span>}</div>;
  if (access !== "ready") return <main ref={root} className="evac-workspace traffic-lab" data-testid="traffic-lab">{heading}
    {error && <div className="error" role="alert">{error}
      <button onClick={lab.reload}>Retry connection</button></div>}
    {!error && <p>Connecting to the simulation workspace…</p>}
  </main>;

  return <main ref={root} className="evac-workspace traffic-lab" data-testid="traffic-lab">{heading}
    {error && <div className="error" role="alert">{error}
      {lab.readStopped && <button onClick={lab.reload}>Retry status read</button>}
      <button onClick={() => setError("")} aria-label="Dismiss error">×</button></div>}
    {roadDiscovery.error && <div className="error" role="alert">{roadDiscovery.error}</div>}
    <div className="workspace"><aside>
      <span className="eyebrow">{result ? "SELECTED SCENARIO" : "01 / DEFINE THE AREA"}</span>
      <h1>{result ? corridorLabel(result.scenario.highway,result.network.selected_route) : "Start at the fire."}</h1>
      {result && <div className="scenario-summary">
        <p>Recorded {new Date((job?.created || 0) * 1000).toLocaleString()}. Your inputs and the recording stay together while you switch views.</p>
        <dl><dt>Fire coordinate</dt><dd>{result.scenario.lat}, {result.scenario.lon}</dd>
          <dt>Study area / exit direction</dt><dd>{miles(result.scenario.radius_km)} radius · {result.scenario.exit_direction}</dd>
          <dt>Demand / crews</dt><dd>{result.scenario.vehicles} vehicles · {result.scenario.people_per_vehicle} people each · {result.scenario.crews} crews</dd>
          <dt>Traffic basis</dt><dd>{result.traffic?.status === "applied" ? "Saved TomTom speed limits" : "Assumed traffic"}</dd>
          <dt>Timing</dt><dd>{result.scenario.duration_minutes}-minute run · {result.scenario.departure_minutes}-minute departure window</dd>
          <dt>Assumed hazard</dt><dd>{feet(result.scenario.hazard_radius_m)} starting at minute {result.scenario.hazard_start_minutes}
            {" · "}{feet(result.scenario.hazard_growth_m_min)}/min growth</dd></dl>
        <button className="secondary full" onClick={() => setEditing(!editing)}>{editing ? "Close scenario editor" : "Edit scenario / start another run"}</button>
      </div>}
      <div hidden={!!result && !editing}>
      <p>Select a road corridor and compare outbound evacuation with incoming fire crews.</p>
      <small>U.S. units · miles, mph, feet and °F</small>
      <div className="presets"><button onClick={() => void loadExample("paradise")}
        disabled={!!busy || running || roadDiscovery.pending}>Paradise · SR 191</button>
        <button onClick={() => void loadExample("malibu")}
          disabled={!!busy || running || roadDiscovery.pending}>Malibu · SR 1</button></div>
      {roadDiscovery.pending && <div className="progress"><div><b role="status">
        {roadDiscovery.job?.phase || "Submitting road discovery…"}</b>
        {roadDiscovery.job && <button disabled={roadDiscovery.job.status === "cancelling" || !!busy}
          onClick={() => void action("cancel discovery", roadDiscovery.cancel)}>
          {roadDiscovery.job.status === "cancelling" ? "Cancelling…" : "Cancel road discovery"}</button>}</div>
        <progress value={roadDiscovery.job?.progress ?? 0} max={1} aria-label="Road discovery progress" />
        <small>The worker prepares real roads for this study area. Slow preparation remains pending.</small></div>}
      {roadDiscovery.job?.status === "cancelled" && <p role="status">Road discovery cancelled.</p>}
      <fieldset disabled={running || !!busy || roadDiscovery.pending}><div className="two">
        <label>Fire latitude<input type="number" step="0.0001" value={scenario.lat}
          onChange={e => moveArea("lat", Number(e.target.value))}/></label>
        <label>Fire longitude<input type="number" step="0.0001" value={scenario.lon}
          onChange={e => moveArea("lon", Number(e.target.value))}/></label></div>
        <UnitInput label="Network radius · miles" value={scenario.radius_km} factor={MILES_PER_KM} min={2} max={15}
          onChange={value => {
            roadDiscovery.clear(); change("radius_km", value); change("highway", ""); setDiscovery(null); clearReplay();
          }}/>
        <button className="secondary full" onClick={() => void action("discover", roadDiscovery.start)}>
          {busy === "discover" || roadDiscovery.pending ? "Finding road corridors…" : "Discover nearby road corridors"}</button>
        <label>Road corridor<select value={scenario.highway} onChange={e => {
          change("highway", e.target.value); clearReplay();
        }}>
          {discovery?.highways?.map(h => <option key={corridorId(h)}
            value={corridorId(h)}>{corridorLabel(corridorId(h),h)} · {miles(h.approx_distance_km ?? NaN)} away</option>)}
          {!discovery && <option>Discover road corridors first</option>}
          {discovery && !discovery.highways.length && <option value="">No usable road corridors returned</option>}
        </select></label><small>{discovery?.source_mode || (discovery ? "Queried road corridor geometry"
          : "Area changed. Discover road corridors before running.")}</small>
        <hr/><span className="eyebrow">02 / SET ASSUMPTIONS</span>
        <div className="two">{numberInput("Evacuation vehicles", "vehicles", 20, 1500)}
          {numberInput("People per vehicle", "people_per_vehicle", 1, 8)}
          {numberInput("Fire-crew vehicles", "crews", 0, 16)}
          {numberInput("Run length · min", "duration_minutes", 5, 60)}
          {numberInput("Departure window · min", "departure_minutes", 1, 45)}
          <label>Assumed exit direction<select value={scenario.exit_direction} onChange={e => change("exit_direction", e.target.value)}>
            {["south", "north", "east", "west"].map(d => <option key={d}>{d}</option>)}</select></label></div>
        <label>Traffic conditions<select value={scenario.traffic_mode} onChange={e => change("traffic_mode", e.target.value)}>
          <option value="synthetic">Use assumed traffic</option>
          <option value="provider_speed_limits" disabled={!config?.traffic_key_configured}>Use current TomTom speeds</option>
        </select></label>
        <small>{config?.traffic_key_configured ? "TomTom key configured. Coverage is checked when you run." : "TomTom key is not configured on this server."}
          {scenario.traffic_mode === "provider_speed_limits" && <> Up to 12 corridor requests per run, shared across policies.
            Matched speeds become fixed speed limits; no measured traffic counts or starting queues are added.
            Provider failures or unusable speeds stop the run. If the shared daily request budget is exhausted,
            the saved result explicitly reports unavailable provider speeds and every road keeps its OSM limit.</>}</small>
        <details><summary>Hazard &amp; reproducibility</summary><div className="two">
          <UnitInput label="Initial radius · feet" value={scenario.hazard_radius_m} factor={FEET_PER_METER}
            min={0} max={3000} onChange={value => change("hazard_radius_m", value)}/>
          <UnitInput label="Growth · feet/min" value={scenario.hazard_growth_m_min} factor={FEET_PER_METER}
            min={0} max={100} onChange={value => change("hazard_growth_m_min", value)}/>
          {numberInput("Hazard begins · min", "hazard_start_minutes", 0, 45)}
          {numberInput("Random seed", "seed", 0, 2147483647)}
        </div>{numberInput("Manual speed factor", "background_speed_factor", 0.3, 1, 0.05)}
          <small>Expanding circle is a scenario assumption. Weather and satellite observations do not predict its growth.</small></details>
        <button className="primary full" disabled={!selectedCorridor} onClick={() => void startRun()}>
          {busy === "run" ? "Creating run…" : "Run three-policy comparison →"}</button>
      </fieldset>
      <small className="run-note">Runs are calculated first, then replayed. A new road network may take several minutes to prepare.
        After completion, press Play below the map. To reopen an earlier result, select a completed run below.</small>
      </div>
      {running && <button className="secondary full" disabled={!!busy} onClick={clearReplay}>Start another research scenario</button>}
      {history.length > 0 && <label className="history">Recent research runs<select value={job?.id || ""}
        disabled={!!busy || roadDiscovery.pending} onChange={e => {
        const id = e.target.value;
        void action("history", async () => { await lab.select(id); setSources(null); setPlaying(false); });
      }}><option value="" disabled>Select a run</option>{history.map(j => <option key={j.id} value={j.id}>
        {corridorLabel(j.scenario.highway)} · {j.status} · {new Date(j.created * 1000).toLocaleTimeString()}</option>)}</select></label>}
      {result && <div className="scenario-guide"><b>Choose a question</b><p>Overview explains the outcome. Bottlenecks shows traffic buildup.
        Fire crews traces dispatch and movement. Data &amp; evidence shows the saved sources and assumptions.</p></div>}
    </aside><div className="stage"><div className="stage-top"><div><span className="eyebrow">CORRIDOR WORKSPACE</span>
      <h2>{corridorLabel(result?.scenario.highway || scenario.highway,
        result?.network.selected_route || discovery?.highways.find(h => corridorId(h) === scenario.highway))}
        <span>{result ? "Experiment replay" : "Network discovery"}</span></h2></div>
      <span className={`badge ${  result ? "available" : ""}`}
        title="A directional road segment is one piece of road in one travel direction; it is not a whole street.">
        {result ? `${result.network.edge_count.toLocaleString()} directional road segments` : running ? "Calculating simulation" : "No replay loaded"}</span></div>
      {result && <>
        <nav className="scenario-nav" aria-label="Scenario views">
          {[["overview", "Overview"], ["bottlenecks", "Bottlenecks"], ["crews", "Fire crews"], ["data", "Data & evidence"]].map(([value, label]) =>
            <button key={value} aria-pressed={focus === value} onClick={() => { setFocus(value); if (value === "data") setPlaying(false); }}>{label}</button>)}
        </nav>
      </>}
      {running && job && <div className="progress"><div><b role="status">{job.phase}</b><button disabled={job.status === "cancelling"}
        onClick={() => void action("cancel", lab.cancel)}>
        {job.status === "cancelling" ? "Cancelling…" : "Cancel run"}</button></div><ProgressExplanation job={job}/>
        <progress value={job.progress} max="1" aria-label="Simulation progress"/>
        <small>{Math.round(job.progress * 100)}% · Run {job.id.slice(0, 8)}</small></div>}
      {focus !== "data" && <ReplayStatus result={result} frame={frame} playing={playing}/>}
      {!result && !running && <div className="replay-status"><b>{lab.loading ? "Loading the saved run…"
        : lab.loadFailed ? "Run results did not load" : job?.status === "cancelled" ? "Run cancelled"
          : job?.status === "failed" ? "Run did not complete" : "Ready to explore a scenario"}</b>
        {lab.loadFailed ? <span>The run completed, but its saved replay could not be downloaded.{" "}
          <button className="secondary" onClick={lab.reload}>Load results again</button></span>
          : <span>Choose a road corridor and click “Run three-policy comparison” to calculate a new simulation, or select a completed entry under Recent runs to replay it.</span>}</div>}
      <div hidden={!!result && focus === "data"} ref={fullscreen.containerRef}
        className={fullscreen.expanded ? "map-undocked" : undefined}
        role={fullscreen.expanded ? "dialog" : undefined}
        aria-modal={fullscreen.expanded ? true : undefined} aria-label="Research scenario map">
      <div className="scenario-toolbar">
        {result && <div className="policy-switch" role="group" aria-label="Policy to replay">
          {result.policies.map((p, i: number) => <button key={p.policy} aria-pressed={i === policyIndex}
            onClick={() => selectPolicy(i)}><i style={{ background: POLICY_COLORS[i] }}/>{policyNames[p.policy]}</button>)}
        </div>}
        <button className="secondary" data-map-fullscreen aria-expanded={fullscreen.expanded}
          onClick={fullscreen.toggle}>{fullscreen.expanded ? "Restore research map" : "Enlarge research map"}</button>
        {result && <small>Computed by {result.engine} {result.engine_version}. Public observations are a saved context snapshot.</small>}
      </div>
      {!result && <SourcePreview result={result} sources={sources} inspect={() => void inspectSources()} busy={busy === "sources"}/>}
      <MapPanel id={MAP_ID} scenario={result?.scenario || scenario} discovery={discovery} network={network} result={result}
        policy={current} frame={frame} sources={sources} onInspect={() => setPlaying(false)}
        focus={focus} focusRequest={focusRequest} showRoutes={showRoutes} showTrails={showTrails} showTraffic={showTraffic}
        showCongestion={showCongestion}/>
      <div className="playback"><button className="play" disabled={!result} aria-label={playing ? "Pause replay" : "Play replay"}
        onClick={() => { if (current && frameIndex === current.frames.length - 1) setFrameIndex(0); setPlaying(!playing); }}>{playing ? "Ⅱ" : "▶"}</button>
        <div className="timeline"><div><b>{current ? policyNames[current.policy] : "Run a comparison to start replay"}</b>
          <span>{clock(frame?.t || 0)} / {clock((result?.scenario.duration_minutes || scenario.duration_minutes) * 60)}</span></div>
          <input aria-label="Simulation time" type="range" min="0" max={Math.max(0, (current?.frames.length || 1) - 1)}
            value={frameIndex} disabled={!result} onChange={e => { setFrameIndex(Number(e.target.value)); setPlaying(false); }}/></div>
        <select aria-label="Playback speed" value={speed} onChange={e => setSpeed(Number(e.target.value))}>
          {[5, 20, 60, 120].map(s => <option value={s} key={s}>{s}×</option>)}</select></div>
      {result && <div className="situation-controls" role="group" aria-label="Focus on a situation">
        <span>Jump to:</span>
        <button onClick={() => jumpTo(0)}>Start</button>
        <button disabled={result.scenario.hazard_start_minutes > result.scenario.duration_minutes}
          onClick={() => jumpTo(result.scenario.hazard_start_minutes * 60)}>Hazard begins</button>
        <button onClick={() => { setFocus("bottlenecks"); jumpTo(current ? peakSample(current.series).t : 0); }}>Peak slowdown</button>
        <button onClick={() => jumpTo(result.scenario.duration_minutes * 60)}>End of run</button>
        {focus === "crews" && <button onClick={() => jumpTo(current?.frames.find((f) => f.vehicles.some((v: Vehicle) => v[4]))?.t || 0)}>
          First crew on road</button>}
        <span className="control-divider">Map:</span>
        <button onClick={() => setFocusRequest({ kind: "hazard", changed: Date.now() })}>Focus on hazard</button>
        <button onClick={() => setFocusRequest({ kind: "network", changed: Date.now() })}>Whole study area</button>
        <label><input type="checkbox" checked={showCongestion} onChange={e => setShowCongestion(e.target.checked)}/> Show congestion</label>
        {result.traffic?.status === "applied" && <label><input type="checkbox" checked={showTraffic}
          onChange={e => setShowTraffic(e.target.checked)}/> Show saved traffic speeds</label>}
      </div>}
      <MetricCards items={focus === "crews" ? [
        ["Crews on roads", frame?.vehicles.filter((v: Vehicle) => v[4]).length, "Crew vehicles visible at this replay time, including queued and staging vehicles."],
        ["Crews staged", liveMetrics?.crews_staged, "Reached the staging segment by this time; not completed firefighting."],
        ["Crews returned", crewsReturned(frame, current, (result?.scenario.duration_minutes || 0) * 60),
        "Reached an exit gate after staging. Earlier recordings only save the final return total."],
      ] : focus === "bottlenecks" ? [
        ["Slow / stopped", liveMetrics?.queue, "On-road vehicles below about 1.1 mph, including crew stops."],
        ["Not departed", liveMetrics?.not_departed, "Waiting before road entry, including held or unroutable demand."],
        ["Closed segments", liveMetrics?.closed_edges, "Directional road segments closed to new entry by the assumed hazard."],
      ] : [
        ["Model exits", liveMetrics?.exited, "Civilian vehicles that reached an assumed exit by this replay time."],
        ["Still on roads", liveMetrics?.active, "Civilian vehicles in the network, including queues. They have not exited."],
        ["Not departed", liveMetrics?.not_departed, "Civilian vehicles still at origins, including any without a route or held by closures."],
        ["Crews staged", liveMetrics?.crews_staged, "Crews that reached their assigned staging segment; not completed firefighting."],
      ]}/>
      </div>
      {focus === "overview" && <>
        {result && <><MissionFindings result={result} inspect={inspectFinding}/>
          <details><summary>Detailed resident and crew comparison</summary><MissionComparison result={result}/></details>
          <details><summary>Recorded outcomes and export</summary>
            <ReplayExplanation result={result} policy={current} frame={frame} metrics={liveMetrics}/>
            <Comparison result={result} index={policyIndex} setIndex={selectPolicy}/></details></>}
      </>}
      {focus === "bottlenecks" && <>
        <BottleneckExplanation result={result} policy={current} frame={frame}/>
        {result && <PolicyInsights result={result} inspect={inspectPolicy}/>}
      </>}
      {focus === "crews" && <>
        <CrewExplanation result={result} frame={frame}
        showRoutes={showRoutes} showTrails={showTrails} setRoutes={setShowRoutes} setTrails={setShowTrails}
        locate={(id: string, origin?: boolean) => { setPlaying(false); setFocusRequest({ kind: origin ? "crew-origin" : "crew", id, changed: Date.now() }); }}/>
        {result && <MissionComparison result={result} crewOnly/>}</>}
      {result && focus === "data" && <>
        <section className="source-evidence-heading" id="source-evidence" aria-label="Source evidence">
          <span className="eyebrow">PROVIDER DATA · SAVED EVIDENCE</span><h2>Inspect the data behind this run</h2>
          <p>TomTom&apos;s corridor readings below supplied the saved speed constraints when enabled.
            Available transportation, weather and fire feeds provide separate context. Dates describe each source&apos;s snapshot.</p>
          <SourcePreview result={result} sources={sources} inspect={() => void inspectSources()} busy={busy === "sources"}/>
          <button className="secondary" onClick={() => download({
            note: "Original source units are retained. Traffic and road provenance belong to the completed run.",
            public_context: sources === result.sources ? "saved_with_run" : "refreshed_not_used_in_run",
            traffic: result.traffic || null, road_provenance: result.network, public_sources: sources,
          }, "wildfire-source-evidence.json")}>Download source evidence JSON</button>
        </section>
        <ProviderReadings traffic={result.traffic} highway={corridorLabel(result.scenario.highway,result.network.selected_route)} locate={(segment, number: number) => {
          // Same as the Evacuation workspace: show the overlay, then bring the map into view.
          setFocus("overview"); setPlaying(false); setShowTraffic(true);
          setFocusRequest({ kind: "traffic-segment", id: segment.id, number,
            highway: corridorLabel(result.scenario.highway,result.network.selected_route), changed: Date.now() });
          requestAnimationFrame(() => document.getElementById(MAP_ID)?.scrollIntoView({ behavior: "smooth", block: "center" }));
        }}/>
        <RunEvidence result={result} job={job}/>
        <TrafficEvidence traffic={result.traffic}/>
        <SourceCoverage data={sources} saved={result.sources} restore={() => setSources(result.sources)}
          busy={busy === "sources"} refresh={refreshSources}/>
      </>}
      <details className="map-guide-disclosure"><summary>Map symbols · why red roads can extend beyond the hazard circle</summary>
        <MapGuide sources={sources} scenario={result?.scenario || scenario}/></details>
      {!result && !running && <div className="stage-note"><b>What this comparison tests</b><p>Baseline traffic, adaptive routing with staged crew dispatch,
        and phased civilian departures. All three use the same network, demand, and hazard assumptions.</p></div>}
    </div></div>
    {!result && <SourceCoverage data={sources} busy={busy === "sources"} refresh={refreshSources}/>}
    <footer>Traffic Lab · Research prototype · OpenStreetMap and returned source providers
      <span>No feed implies complete coverage or road safety.</span></footer>
  </main>;
}
