/* Agent activity: tool-call status from stream events and receipts, the supervisor's delegation and synthesis
 * rows, and the Simulation agent's broker notices. Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("../ts-module.cjs");
const { browser, brokerOr } = require("./support/browser.cjs");
const { streamOf, within } = require("./support/streams.cjs");
const { DIRECT_TITLE, clock, loadActivity, notice, rowsOf } = require("./support/agent-activity.cjs");

test("tool activity uses actual SDK completion, preserves nested failures and never infers source truth", () => {
  const {toolResultStatus, receiveToolResult, startToolEvent, endToolArguments} = load("shared/services/toolActivity.ts");
  const receipt = {query_type:"current", mapData:{locations:[]}};
  assert.equal(toolResultStatus(JSON.stringify(receipt)), "unverified");
  assert.equal(toolResultStatus(JSON.stringify({status:"success",result:receipt})), "done");
  assert.equal(toolResultStatus({status:"error",result:receipt}), "error");
  assert.equal(toolResultStatus({status:"success",result:{guidance_unavailable:true}}), "error");
  let events = startToolEvent([], "a", "detection_agent_tool", "Detection", 100);
  events = endToolArguments(events, "a");
  assert.equal(events[0].status, "awaiting_result");
  events = receiveToolResult(events, "a", {status:"success",result:receipt}, 300);
  assert.equal(events[0].status, "done");
  assert.equal(events[0].endedAt, 300);
  events = receiveToolResult(events, "a", {status:"error"}, 400);
  events = receiveToolResult(events, "a", {status:"success"}, 500);
  assert.equal(events[0].status, "error");
});

test("text tool receipts: a nested error or isError fails, prose stays unverified, and an early result keeps its status", () => {
  const { toolResultStatus, startToolEvent, endToolArguments, receiveToolResult, closeOpenEvents } =
    load("shared/services/toolActivity.ts");
  // Waiting for a result is not an ending.
  let events = endToolArguments(startToolEvent([], "a", "geocode", "Resolve address", 0), "a");
  assert.deepEqual([events[0].status, events[0].endedAt], ["awaiting_result", undefined]);
  // A failure inside a success envelope's text is a failure, and a later success receipt does not undo it.
  events = receiveToolResult(events, "a", JSON.stringify({
    status: "success", content: [{ text: JSON.stringify({ error: "Provider unavailable" }) }],
  }), 20);
  assert.equal(events[0].status, "error");
  assert.equal(receiveToolResult(events, "a", '{"success":true}', 21)[0].status, "error");
  for (const [receipt, status] of [["plain narrative without a result receipt", "unverified"], ["not json", "unverified"],
    ['{"status":"success","content":[{"text":"ok"}]}', "done"], ['{"isError":true,"content":[]}', "error"]]) {
    assert.equal(toolResultStatus(receipt), status, receipt);
  }
  // A call still open when the run ends is closed as unverified.
  assert.equal(closeOpenEvents(startToolEvent([], "b", "tool", "Tool", 0), "unverified", 30)[0].status, "unverified");
  // A result that arrives before its start keeps its status and takes the start's label.
  const lateStart = startToolEvent(receiveToolResult([], "c", '{"success":true}', 5), "c", "weather", "Read weather", 10);
  assert.deepEqual([lateStart[0].label, lateStart[0].status], ["Read weather", "done"]);
  assert.equal(endToolArguments(lateStart, "c")[0].status, "done");
});

test("supervisor activity needs run evidence and recognized supervisor tools", () => {
  const { supervisorActivity, startSupervisorRun, call } = loadActivity();
  const detection = call("d", "detection_agent_tool", "done", 110, 150);
  assert.equal(supervisorActivity([detection], undefined), null);
  const run = startSupervisorRun(100);
  for (const other of [call("p", "prepare_simulation", "running", 120), call("a", "AnswerResponse", "done", 120, 130),
    {id:"o", name:"", label:"Tool response", startedAt:120, endedAt:120, status:"unverified"}]) {
    assert.equal(supervisorActivity([other], run), null, other.label);
    assert.equal(supervisorActivity([detection, other], run), null, other.label);
  }
});

test("delegation rows follow the supervisor's specialist calls in arrival order", () => {
  const { supervisorActivity, startSupervisorRun, call } = loadActivity();
  const events = [call("d", "detection_agent_tool", "done", 110, 150),
    call("g", "public_response_agent_tool", "awaiting_result", 160)];
  const view = supervisorActivity(events, startSupervisorRun(100));
  assert.equal(view.startedAt, 100);
  assert.deepEqual(view.steps, [{kind:"delegation", event:events[0]}, {kind:"delegation", event:events[1]}]);
  assert.deepEqual(view.steps.map(step => step.event.label),
    ["Detection agent: checking requested observations", "Guidance agent: finding published guidance"]);
});

test("synthesis starts only at the completion call and counts distinct specialists", () => {
  const { supervisorActivity, startSupervisorRun, endSupervisorRun, call } = loadActivity();
  const events = [call("d1", "detection_agent_tool", "done", 110, 120), call("d2", "detection_agent_tool", "done", 130, 140),
    call("g", "public_response_agent_tool", "done", 150, 170), call("f", "PublicAnswerResponse", "running", 180)];
  const live = supervisorActivity(events, startSupervisorRun(100));
  assert.deepEqual(live.steps.map(step => step.kind), ["delegation", "delegation", "delegation", "synthesis"]);
  const combining = live.steps[3];
  assert.equal(combining.status, "in_progress");
  assert.equal(combining.label, "Supervisor: combining results from 2 specialists");
  assert.equal(combining.startedAt, 180);
  assert.equal(combining.endedAt, undefined);
  const combined = supervisorActivity(events, endSupervisorRun(startSupervisorRun(100), "completed", 900)).steps[3];
  assert.equal(combined.status, "completed");
  assert.equal(combined.label, "Supervisor: combined results from 2 specialists into the answer");
  assert.equal(combined.startedAt, 180);
  assert.equal(combined.endedAt, 900);
  // A failed result still reaches the answer; a result that never arrived is not counted.
  const failedGuidance = [call("g", "public_response_agent_tool", "error", 110, 120),
    call("f", "PublicAnswerResponse", "running", 130)];
  assert.equal(supervisorActivity(failedGuidance, startSupervisorRun(100)).steps[1].label,
    "Supervisor: combining results from 1 specialist");
  const unverified = [call("d", "detection_agent_tool", "unverified", 110, 120), call("f", "PublicAnswerResponse", "running", 130)];
  assert.equal(supervisorActivity(unverified, startSupervisorRun(100)).steps[1].label, "Supervisor: preparing the answer");
  assert.equal(supervisorActivity(unverified, endSupervisorRun(startSupervisorRun(100), "completed", 200)).steps[1].label,
    "Supervisor: completed the answer");
});

test("two specialist calls open at once show side by side in start order and both reach the synthesis", () => {
  const { supervisorActivity, startSupervisorRun, endSupervisorRun, startToolEvent, endToolArguments, receiveToolResult,
    labelForTool, render } = loadActivity();
  // Two calls open at once: a model turn that requests several tools streams each START and END before any RESULT.
  let events = [];
  for (const [id, name, at] of [["d", "detection_agent_tool", 110], ["g", "public_response_agent_tool", 115]]) {
    events = endToolArguments(startToolEvent(events, id, name, labelForTool(name), at), id);
  }
  const run = startSupervisorRun(100);
  /** Each step as [kind, tool name, status]; a synthesis step has no tool name. */
  const order = () => supervisorActivity(events, run).steps.map(step => step.kind === "synthesis"
    ? [step.kind, undefined, step.status] : [step.kind, step.event.name, step.event.status]);
  assert.deepEqual(order(), [["delegation", "detection_agent_tool", "awaiting_result"],
    ["delegation", "public_response_agent_tool", "awaiting_result"]]);
  assert.deepEqual(rowsOf(render({events, run})).map(row => row.slice(0, 4)), [
    ["supervisor", undefined, "Supervisor: received the request", undefined],
    ["delegation", "awaiting_result", "Detection agent: checking requested observations", "Waiting for result"],
    ["delegation", "awaiting_result", "Guidance agent: finding published guidance", "Waiting for result"]]);
  // Results arriving in reverse order keep the start order.
  events = receiveToolResult(events, "g", {status:"success"}, 140);
  assert.deepEqual(order(), [["delegation", "detection_agent_tool", "awaiting_result"],
    ["delegation", "public_response_agent_tool", "done"]]);
  events = receiveToolResult(events, "d", {status:"success"}, 150);
  assert.deepEqual(order(), [["delegation", "detection_agent_tool", "done"],
    ["delegation", "public_response_agent_tool", "done"]]);
  events = startToolEvent(events, "f", "PublicAnswerResponse", labelForTool("PublicAnswerResponse"), 160);
  const synthesis = supervisorActivity(events, run).steps;
  assert.deepEqual(synthesis.map(step => step.kind), ["delegation", "delegation", "synthesis"]);
  assert.deepEqual([synthesis[2].status, synthesis[2].label, synthesis[2].startedAt, synthesis[2].endedAt],
    ["in_progress", "Supervisor: combining results from 2 specialists", 160, undefined]);
  assert.deepEqual(rowsOf(render({events, run:endSupervisorRun(run, "completed", 200)})).map(row => row.slice(0, 4)), [
    ["supervisor", undefined, "Supervisor: received the request", undefined],
    ["delegation", "done", "Detection agent: checking requested observations", "Response received"],
    ["delegation", "done", "Guidance agent: finding published guidance", "Response received"],
    ["synthesis", "done", "Supervisor: combined results from 2 specialists into the answer", "Completed"]]);
});

test("no specialist call means the supervisor answered directly", () => {
  const { supervisorActivity, startSupervisorRun, endSupervisorRun, call } = loadActivity();
  const completed = endSupervisorRun(startSupervisorRun(100), "completed", 140);
  const summary = view => view.steps.map(step => [step.kind, step.status, step.label]);
  assert.deepEqual(summary(supervisorActivity([call("f", "PublicAnswerResponse", "done", 120, 130)], completed)),
    [["synthesis", "completed", "Supervisor: answered directly"]]);
  assert.deepEqual(summary(supervisorActivity([call("f", "PublicAnswerResponse", "running", 120)], startSupervisorRun(100))),
    [["synthesis", "in_progress", "Supervisor: answering directly"]]);
  assert.deepEqual(summary(supervisorActivity([], completed)), [["synthesis", "completed", "Supervisor: answered directly"]]);
});

test("failed, cancelled and interrupted runs never read as completed", () => {
  const { supervisorActivity, startSupervisorRun, endSupervisorRun, call } = loadActivity();
  const events = [call("d", "detection_agent_tool", "done", 110, 120), call("f", "PublicAnswerResponse", "running", 130)];
  for (const status of ["failed", "cancelled", "interrupted"]) {
    const { steps } = supervisorActivity(events, {status, startedAt:100, endedAt:200});
    assert.deepEqual(steps.map(step => step.kind), ["delegation", "synthesis"], status);
    assert.equal(steps[1].status, status);
    assert.equal(steps[1].label, "Supervisor: answer not completed");
  }
  const empty = supervisorActivity([], endSupervisorRun(startSupervisorRun(100), "failed", 150));
  assert.equal(empty.startedAt, 100);
  assert.deepEqual(empty.steps.map(step => [step.kind, step.status, step.label]),
    [["synthesis", "failed", "Supervisor: answer not completed"]]);
  assert.equal(empty.steps[0].startedAt, undefined);
  assert.equal(empty.steps[0].endedAt, 150);
});

test("a call still open when its run ended reads Completion status unavailable, with no invented end time", () => {
  const { supervisorActivity, startSupervisorRun, savedSupervisorRun, call, render } = loadActivity();
  // The failed communication call checks that closing a run never hides a specialist that failed.
  const events = [call("d", "detection_agent_tool", "running", 110), call("g", "public_response_agent_tool", "awaiting_result", 120),
    call("c", "communication_agent_tool", "error", 1000, 2000)];
  /** Each step as [kind, status] plus a call's end time. */
  const statuses = view => view.steps.map(step => step.kind === "synthesis" ? [step.kind, step.status]
    : [step.kind, step.event.status, step.event.endedAt]);
  // While the run is open, each call keeps its own state.
  assert.deepEqual(statuses(supervisorActivity(events, startSupervisorRun(100))),
    [["delegation", "running", undefined], ["delegation", "awaiting_result", undefined], ["delegation", "error", 2000]]);
  for (const status of ["failed", "cancelled", "interrupted", "completed"]) {
    const run = savedSupervisorRun({status, completed_at:12, activity:events});
    assert.deepEqual(statuses(supervisorActivity(events, run)), [["delegation", "unverified", undefined],
      ["delegation", "unverified", undefined], ["delegation", "error", 2000], ["synthesis", status]], status);
    const html = render({events, run});
    assert.ok(!html.includes("Calling tool") && !html.includes("Waiting for result"), status);
    const rows = rowsOf(html);
    assert.deepEqual(rows.slice(1, 3), [
      ["delegation", "unverified", "Detection agent: checking requested observations", "Completion status unavailable",
        `Delegated by Supervisor · ${clock(110)}`],
      ["delegation", "unverified", "Guidance agent: finding published guidance", "Completion status unavailable",
        `Delegated by Supervisor · ${clock(120)}`]], status);
    assert.deepEqual(rows[3], ["delegation", "error", "Communication agent: preparing multilingual voice drafts", "Failed",
      `Delegated by Supervisor · ${clock(1000)} · 1.0s`], status);
  }
  assert.deepEqual(events.map(event => event.status), ["running", "awaiting_result", "error"],
    "Recorded events are not changed");
});

test("a rejected completion attempt stays visible as failed while the supervisor continues", () => {
  const { supervisorActivity, startSupervisorRun, call } = loadActivity();
  const detection = call("d", "detection_agent_tool", "done", 110, 120);
  const rejected = call("f1", "PublicAnswerResponse", "error", 130, 140);
  const continued = supervisorActivity([detection, rejected, call("g", "public_response_agent_tool", "running", 150)],
    startSupervisorRun(100));
  assert.deepEqual(continued.steps.map(step => [step.kind, step.event.status]),
    [["delegation", "done"], ["completion_attempt", "error"], ["delegation", "running"]]);
  assert.equal(continued.steps[1].event.label, "Preparing the final response");
  assert.deepEqual(supervisorActivity([detection, rejected], startSupervisorRun(100)).steps.map(step => step.kind),
    ["delegation", "completion_attempt"]);
});

test("saved tasks map to run state without inventing a start time", () => {
  const { savedSupervisorRun } = loadActivity();
  const activity = [{tool_call_id:"d", name:"detection_agent_tool", status:"result_received", started_at:10, ended_at:11}];
  assert.equal(savedSupervisorRun(null), undefined);
  assert.equal(savedSupervisorRun({status:"completed", completed_at:12, activity:[]}), undefined);
  assert.equal(savedSupervisorRun({status:"queued", completed_at:null, activity}), undefined);
  assert.deepEqual(savedSupervisorRun({status:"completed", completed_at:12, activity}), {status:"completed", endedAt:12000});
  assert.deepEqual(savedSupervisorRun({status:"running", completed_at:null, activity}), {status:"running"});
  assert.deepEqual(savedSupervisorRun({status:"failed", activity}), {status:"failed"});
  for (const status of ["interrupted", "cancelled"]) {
    assert.deepEqual(savedSupervisorRun({status, completed_at:12, activity}), {status, endedAt:12000});
  }
});

test("live run state starts at RUN_STARTED and the first terminal signal wins", () => {
  const { startSupervisorRun, endSupervisorRun } = loadActivity();
  assert.deepEqual(startSupervisorRun(5), {status:"running", startedAt:5});
  assert.equal(endSupervisorRun(undefined, "failed", 9), undefined);
  assert.deepEqual(endSupervisorRun(undefined, "completed", 9), {status:"completed", endedAt:9});
  const completed = endSupervisorRun(startSupervisorRun(5), "completed", 9);
  assert.deepEqual(completed, {status:"completed", startedAt:5, endedAt:9});
  assert.equal(endSupervisorRun(completed, "failed", 12), completed);
  const failed = endSupervisorRun(startSupervisorRun(5), "failed", 9);
  assert.deepEqual(failed, {status:"failed", startedAt:5, endedAt:9});
  assert.equal(endSupervisorRun(failed, "completed", 12), failed);
});

test("agent activity renders the supervisor hierarchy as nested lists with text statuses", () => {
  const { startSupervisorRun, endSupervisorRun, call, render } = loadActivity();
  const events = [call("d", "detection_agent_tool", "done", 1000, 2000),
    call("g", "public_response_agent_tool", "done", 2100, 3000), call("f", "PublicAnswerResponse", "result_received", 3100, 3200)];
  const html = render({events, run:endSupervisorRun(startSupervisorRun(900), "completed", 3600)});
  assert.match(html, new RegExp('^<section class="agent-activity" aria-label="Agent activity"><h3>Agent activity</h3>'
    + '<ol><li data-kind="supervisor"><div><strong>Supervisor: received the request</strong></div><small>[^<]+</small>'
    + '<ol aria-label="Supervisor delegation and synthesis"><li .+</li></ol></li></ol></section>$'));
  assert.deepEqual(rowsOf(html), [
    ["supervisor", undefined, "Supervisor: received the request", undefined, clock(900)],
    ["delegation", "done", "Detection agent: checking requested observations", "Response received",
      `Delegated by Supervisor · ${clock(1000)} · 1.0s`],
    ["delegation", "done", "Guidance agent: finding published guidance", "Response received",
      `Delegated by Supervisor · ${clock(2100)} · 0.9s`],
    ["synthesis", "done", "Supervisor: combined results from 2 specialists into the answer", "Completed",
      `${clock(3100)} · 0.5s`],
  ]);
  const midRun = render({events:[events[0], call("g", "public_response_agent_tool", "awaiting_result", 2100)],
    run:startSupervisorRun(900)});
  assert.deepEqual(rowsOf(midRun).map(row => row.slice(0, 4)), [
    ["supervisor", undefined, "Supervisor: received the request", undefined],
    ["delegation", "done", "Detection agent: checking requested observations", "Response received"],
    ["delegation", "awaiting_result", "Guidance agent: finding published guidance", "Waiting for result"],
  ]);
  assert.deepEqual(rowsOf(render({events:[call("f", "PublicAnswerResponse", "error", 1000, 1500)], run:startSupervisorRun(900)}))[1],
    ["completion_attempt", "error", "Preparing the final response", "Failed", `Supervisor · ${clock(1000)} · 0.5s`]);
  for (const [status, dataStatus, word] of [["failed", "error", "Failed"], ["cancelled", "cancelled", "Cancelled"],
    ["interrupted", "interrupted", "Interrupted"]]) {
    // A saved task records when it ended but not when the supervisor started.
    const rows = rowsOf(render({events, run:{status, endedAt:3600}}));
    assert.deepEqual(rows[0], ["supervisor", undefined, "Supervisor: received the request", undefined, undefined]);
    assert.deepEqual(rows[3], ["synthesis", dataStatus, "Supervisor: answer not completed", word, `${clock(3100)} · 0.5s`]);
  }
});

test("agent activity without run evidence renders the existing flat list", () => {
  const { call, render, savedSupervisorRun } = loadActivity();
  const events = [call("d", "detection_agent_tool", "result_received", 1000, 2000),
    call("g", "public_response_agent_tool", "running", 2100)];
  const flat = '<section class="agent-activity" aria-label="Agent activity"><h3>Agent activity</h3><ol>'
    + '<li data-status="result_received"><div><strong>Detection agent: checking requested observations</strong>'
    + `<span>Response received</span></div><small>${clock(1000)} · 1.0s</small></li>`
    + '<li data-status="running"><div><strong>Guidance agent: finding published guidance</strong>'
    + `<span>Calling tool</span></div><small>${clock(2100)}</small></li></ol></section>`;
  assert.equal(render({events}), flat);
  assert.equal(render({events, run:savedSupervisorRun({status:"queued", activity:events})}), flat);
  const simulation = render({events:[...events, call("p", "prepare_simulation", "done", 2200, 2300)],
    run:{status:"running", startedAt:900}});
  assert.equal(simulation.match(/<ol>/g).length, 1);
  assert.ok(!simulation.includes("Supervisor"), simulation);
  assert.equal(render({events:[]}), '<section class="agent-activity" aria-label="Agent activity"><h3>Agent activity</h3>'
    + "<p>Actions appear here when the agent starts using tools.</p></section>");
});

test("tool arguments and markup in the stream never reach Agent activity", async () => {
  const { readFinalAgentSnapshot } = load("shared/services/AgentEventStream.ts");
  const kit = loadActivity();
  const frames = [{type:"RUN_STARTED"}, {type:"TOOL_CALL_START", toolCallId:"d", toolCallName:"detection_agent_tool"},
    {type:"TOOL_CALL_ARGS", toolCallId:"d", delta:JSON.stringify({query:"<b>signed-context-token</b>"})},
    {type:"TOOL_CALL_END", toolCallId:"d"},
    {type:"TOOL_CALL_RESULT", toolCallId:"d", content:JSON.stringify({status:"success", result:{query_type:"current"}})},
    {type:"STATE_SNAPSHOT", snapshot:{type:"structuredResponse", content:{response:"Fixture answer."}}}, {type:"RUN_FINISHED"}];
  let events = [], run;
  // The reducer calls AgentService and useAgentSubmission make; completion waits for the strict final snapshot.
  await readFinalAgentSnapshot(streamOf(...frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`)), {
    onRunStarted: () => { run = kit.startSupervisorRun(); },
    onToolStart: (id, name) => { events = kit.startToolEvent(events, id, name, kit.labelForTool(name)); },
    onToolEnd: id => { events = kit.endToolArguments(events, id); },
    onToolResult: (id, content) => { events = kit.receiveToolResult(events, id, content); },
  });
  events = kit.closeOpenEvents(events, "unverified");
  run = kit.endSupervisorRun(run, "completed");
  const html = kit.render({events, run});
  assert.ok(html.includes("Detection agent: checking requested observations"), html);
  assert.ok(html.includes("Supervisor: combined results from 1 specialist into the answer"), html);
  assert.ok(!html.includes("signed-context-token") && !html.includes("<b>"), html);
  const hostile = "<img src=x onerror=alert(1)>";
  const escaped = kit.render({events:kit.startToolEvent([], "x", hostile, kit.labelForTool(hostile), 1000), run});
  assert.ok(escaped.includes("&lt;img") && !escaped.includes("<img"), escaped);
  assert.ok(!escaped.includes("Supervisor"), "An unrecognized tool keeps the flat list");
});

test("an investigation turn posts its thread and question, and the browser sends no context envelope", async () => {
  const frames = [{type:"RUN_STARTED"},
    {type:"STATE_SNAPSHOT", snapshot:{type:"structuredResponse", content:{response:"Fixture answer."}}}, {type:"RUN_FINISHED"}];
  const env = browser("https://lab.example.test/", brokerOr(() => new Response(
    streamOf(...frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`)), {headers:{"Content-Type":"text/event-stream"}})));
  const { invokeAgentStreaming } = load("shared/services/AgentService.ts");
  const outcome = await within(new Promise(resolve => invokeAgentStreaming("Show current fire weather.", {
    onComplete: answer => resolve({answer}), onError: error => resolve({error}),
  })), 5000, "The investigation turn's answer");
  assert.equal(outcome.error, undefined);
  assert.equal(outcome.answer.response, "Fixture answer.");
  const turns = env.requests.filter(request => !request.url.endsWith("/workspace-config"));
  assert.deepEqual(turns.map(request => [request.url, request.init.method]),
    [["/simulation-api/v1/investigation/turns", "POST"]]);
  // The broker forwards only retained observations (agent_broker.py), so the browser builds no context of its own.
  const body = JSON.parse(turns[0].init.body);
  assert.match(body.request_id, /^[0-9a-f-]{36}$/);
  assert.deepEqual(body, {request_id: body.request_id, thread_id: sessionStorage.getItem("agentcore_session_id"),
    question: "Show current fire weather.", context_envelope: null});
});

test("simulation-agent activity comes only from the broker's names and call ids", () => {
  const { receiveSimulationActivity, closeOpenEvents, render } = loadActivity();
  let events = receiveSimulationActivity([], notice("TOOL_CALL_START"), 1000);
  events = receiveSimulationActivity(events, notice("TOOL_CALL_END"), 1500);
  assert.equal(events[0].status, "awaiting_result");
  events = receiveSimulationActivity(events, notice("TOOL_CALL_RESULT"), 2000);
  assert.deepEqual(events, [{ id: "r1", name: "get_simulation_evidence", label: "Reading retained simulation evidence",
    startedAt: 1000, endedAt: 2000, status: "result_received" }]);
  const html = render({ events, title: DIRECT_TITLE });
  assert.deepEqual(rowsOf(html), [[undefined, "result_received", "Reading retained simulation evidence",
    "Response received", `${clock(1000)} · 1.0s`]]);
  assert.ok(html.includes(`aria-label="${DIRECT_TITLE}"`) && !html.includes("Supervisor"), html);
  for (const ignored of [
    { type: "CUSTOM", name: "simulation_tool_result", value: { tool_name: "get_simulation_evidence", status: "completed" } },
    notice("TOOL_CALL_RESULT", { tool_call_id: undefined }), notice("TOOL_CALL_RESULT", { tool_call_id: 7 }),
    notice("TOOL_CALL_PROGRESS"), notice("TOOL_CALL_START", { tool_call_id: "r2", tool_name: null }),
    notice("TOOL_CALL_END", { tool_call_id: "other" }), notice("TOOL_CALL_RESULT", { tool_call_id: "other" }),
    { type: "CUSTOM", name: "simulation_activity", value: "TOOL_CALL_START" }, { type: "CUSTOM", name: "simulation_activity" },
  ]) assert.equal(receiveSimulationActivity(events, ignored, 3000), events, JSON.stringify(ignored));
  // A late result notice never turns a failed call into a received response.
  const failed = closeOpenEvents(receiveSimulationActivity([], notice("TOOL_CALL_START"), 1000), "error", 1200);
  assert.equal(receiveSimulationActivity(failed, notice("TOOL_CALL_RESULT"), 1300)[0].status, "error");
  const hostile = "<img src=x onerror=alert(1)>";
  const markup = render({ events: receiveSimulationActivity([], notice("TOOL_CALL_START", { tool_name: hostile }), 1000),
    title: DIRECT_TITLE });
  assert.ok(markup.includes("&lt;img") && !markup.includes("<img"), markup);
});
