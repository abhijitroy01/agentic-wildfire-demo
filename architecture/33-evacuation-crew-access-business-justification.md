# Business justification: evacuation and fire-crew access

**Product:** FireGUard Evacuation & Crew Access and Traffic Lab (formerly Wildfire Corridor Lab)  
**Purpose established:** September 17, 2026  
**Audience:** emergency-management partners, fire-service and transportation planners, business sponsors, product teams and developers

## The reason this project exists

**Help people leave areas threatened by wildfire more efficiently while helping fire crews reach the incident efficiently and with less exposure. Evaluate these two objectives together.**

During a rapidly developing wildfire, residents moving outward and responders moving inward depend on the same limited road system. Delays, inaccessible routes, conflicting movements and downstream bottlenecks can have life-threatening consequences. An evacuation plan that improves outbound traffic while preventing responders from reaching people or staging areas is incomplete. A responder-access plan that increases civilian exposure is also incomplete.

Wildfire Corridor Lab exists to make these conflicts visible before an emergency, test plausible alternatives, and help planners understand where an approach works and where it fails. A moving map, a shorter route or a smaller queue is not the business outcome. The outcome sought is better coordinated evacuation and responder access.

The intended public value is reduced exposure, more effective access for rescue and fire-control work, and stronger emergency planning. **The current prototype has not demonstrated lives saved, faster fire containment or operational safety.** Those are desired eventual impacts, not results that can be inferred from the present simulation.

FireGUard AI is a demonstration of what an agentic system can do, not an operational system. For one incident, an assistant works through three stages: investigate what is happening, test how evacuation and crew access interact, and follow how response resources would operate. It gathers evidence, uses simulation to explore alternatives and explains the consequences. The apparatus, crews and assignments are synthetic illustrations. A customer that wants to build its own agentic system from this idea could map its vehicle telemetry and GIS assets into the same three stages, so its trucks and crews can maintain those assets. That would be a customer build; this demonstration does not ingest either source or design that integration.

## Why the need is real

NIST's investigation of the 2018 Camp Fire documents approximately 40,000 evacuees and 85 fatalities. Its report examines notification, evacuation, traffic, refuge, rescues and fatalities together. It describes rapidly changing fire conditions, congestion, blocked roads and interacting civilian/responder movements. In particular, the pre-incident contraflow plan did not specifically address bringing large numbers of responders into town against evacuating traffic (TN 2252, printed page 30).

The report also documents downstream road convergence outside Paradise (page 108) and physical access problems including abandoned vehicles, large vehicles, utility lines and opposing movements (page 115). This evidence supports testing the whole connected road system and the needs of both groups. It does **not** establish that any single traffic-management failure caused every death, or that this application would have prevented those deaths.

NIST's ESCAPE guidance frames evacuation around the time needed to leave compared with the time available before dangerous exposure, including notification and preparation delays. This is a future validation requirement for the project: a model exit count alone is insufficient evidence of safe evacuation.

Sources, already reviewed and preserved in this project's research:

- [NIST Camp Fire life-safety study overview](https://www.nist.gov/programs-projects/wildland-urban-interface-wui-fire-data-collection-parcel-vulnerabilities/nist/life).
- [NIST Technical Note 2252](https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.2252.pdf), especially pages 30, 108 and 115.
- [NIST ESCAPE guidance, TN 2262r1](https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.2262r1.pdf).
- [Project research and source register](44-traffic-simulator-research-notes.md), including local source extracts.

Source review on September 17, 2026 checked these statements against the preserved TN 2252 and TN 2262r1 text extracts, including TN 2252's contraflow limitation on printed page 30 and downstream-convergence figure on page 108. This was a review of the cited incident evidence, not a validation of this prototype's outcomes.

## Who benefits and what decisions they need to make

| User | Decision the project should help investigate |
|---|---|
| Emergency-management planners | Which departure and route strategies allow more residents to leave before access deteriorates? Who remains unable to leave? |
| Fire-service planners | Can assigned crews enter, reach staging on time and retain an exit path? Where do waiting, travel or closures prevent arrival? |
| Transportation and traffic-control teams | Which intersections, road segments and downstream merges constrain both movements? Does an intervention relocate a queue or reduce the underlying problem? |
| Business sponsors | Which planning capabilities and data investments address the largest demonstrated gaps? Is the evidence strong enough for the next validation stage? |
| Developers and analysts | Can every claimed improvement be traced to matched inputs, recorded events, explicit assumptions and reproducible calculations? |

This is initially a planning and research tool. It is not an evacuation-order system, navigation service, live dispatch console or autonomous incident commander.

## What success means in the product

The comparison must lead with two equally visible outcome groups. Congestion explains outcomes; it does not replace them.

| Objective | Measures available now | Interpretation |
|---|---|---|
| Residents leave the modeled area | Civilian exits and corresponding people; unresolved demand; people not departed; time to 95% of requested vehicle exits | More exits and fewer unresolved people are favorable within the same scenario and time horizon. A gate is not a verified refuge. |
| Reduce resident exposure | Vehicles intersecting the assumed hazard and their entered occupancy | Lower is favorable in this model. This is neither an injury probability nor a fatality estimate. |
| Crews get into position | Crews reaching staging, crews not reaching staging, cumulative staging arrivals over time | More arrivals and earlier arrivals can support access. Staging is not suppression, rescue completion or containment. |
| Crews respond efficiently | Scheduled-dispatch-to-staging time, separated into waiting before road entry and travel after entry, for the same crews arriving in both policies | A shorter trip must not hide a longer dispatch wait. Paired arrival comparisons must show their sample size and excluded nonarrivals. |
| Preserve crew safety and access | Crew exposure, held/nondeparted crews, missing routes, staging and return states | Faster arrival cannot compensate silently for greater exposure or inability to arrive. Return is a separate task milestone, not proof of mission success. |
| Explain traffic constraints | Slow vehicle-minutes, peak slow/stopped count, locations and time of buildup, closures | Lower congestion may reflect people being held at origins or crews never entering. Always read it alongside the two outcome groups. |

People counts are derived from the entered occupancy, not independently measured population. Counts from different outcome categories overlap; do not sum exposure and unresolved people as if they were separate populations.

## What “real data” means

| Category | Current role |
|---|---|
| Radio audio | A prerecorded report; its address supplies the scenario location. Audio time is not current incident time. |
| Amazon Location | Resolves addresses/places to coordinates. A location match does not establish current fire activity. |
| TomTom | Samples current road-segment speeds and applies accepted matches as fixed experimental speed limits for a run. |
| Caltrans | Optional California context such as closures and camera inventory; travel-time coverage varies. It does not gate U.S. road preparation. |
| OSM | Supplies the modeled road network. Cached network age and inferred road attributes remain visible. |
| Weather/fire sources | Current contextual observations, with provider and time identified. They do not automatically drive the assumed hazard. |
| Civilian and crew demand | Synthetic scenario inputs, not measured traffic counts or actual dispatch assignments. |
| Hazard | An assumed expanding exclusion area, not a weather/fuel-driven wildfire forecast. |
| Results | Computed SUMO outcomes under the documented inputs and assumptions. |

A fresh standalone run fetches current traffic. A controlled follow-up experiment reuses the parent's saved traffic snapshot, so changing conditions do not confound the parameter comparison. The UI explains this distinction.

## How alternatives must be compared

Baseline is a reference, not an approved operational plan. It uses initial civilian routes and fixed crew dispatch schedules. Coordinated changes civilian routing and permits limited crew dispatch waiting. Phased adds distance-related civilian departure timing to those coordinated rules. The alternatives are heuristic experiments; neither has a guaranteed advantage.

For each alternative, the interface must show absolute baseline and alternative values, a clearly labeled difference, and whether that particular measure improved, worsened or stayed unchanged. The result must retain residents who never departed and crews who never arrived. Missing evidence must display as unavailable, not as zero, success or failure inferred without data.

Use the same network, civilian demand, crew assignments, hazard, traffic snapshot and time horizon within a comparison. Keep intentional policy differences explicit. Distinguish final-run outcomes from the selected replay instant, and inspect different policies at the same simulated time. Full-run timing summaries must not be mistaken for events that have already happened at an earlier replay instant.

Do not collapse the two objectives into an unexplained “best policy” score. For example:

- More people exit but crews arrive later: show the evacuation gain and the response setback.
- Smaller road queues but more people remain at origins: do not claim easier evacuation.
- Lower average crew arrival time but fewer crews arrive: show nonarrivals; compare the same arriving crews and disclose that this subset cannot represent every assignment.
- More crews stage but more residents or crews encounter the hazard: show the exposure cost.
- All measured outcomes tie: say so; do not manufacture a winner.

Any eventual operational acceptance thresholds or weighting between objectives must be defined with incident authorities and validated for the intended use. Single-seed results do not establish statistically reliable improvements.

## Why invest, and what the current product can support

The business case is the opportunity to discover planning failures before they become emergency failures. Reproducible scenario comparisons can support tabletop exercises, reveal route and dispatch assumptions that require field review, and direct scarce data and engineering effort toward consequential bottlenecks.

The current product can demonstrate these workflows using OSM roads and SUMO movement, synthetic civilian and crew demand, assumed hazard growth, and optional frozen TomTom speed constraints. Public closure, fire and weather feeds provide documented context. It can expose a tradeoff, preserve the evidence and make the result understandable to nontechnical reviewers.

No monetary return, casualty reduction or containment improvement has been measured. Funding decisions should initially use evidence of planning usefulness: can practitioners identify a failure mode, trace its assumptions, compare alternatives and define a testable corrective action?

| Expected value | Product evidence to collect |
|---|---|
| Faster access to a coherent planning experiment | Measure time from accepted location to visible job submission and completed replay; separate source preparation from computation. |
| Better understanding of tradeoffs | In user sessions, ask reviewers to identify a resident gain and a crew limitation using the interface. |
| More useful tabletop iteration | Record whether a user can create a supported variant and correctly explain what changed. |
| Greater trust in AI output | Check that claims link to evidence, users can inspect source coverage, and unsupported requests are handled honestly. |
| Less duplicated analysis effort | Verify that reopening a run preserves results and context without unnecessary recomputation. |
| Credible technical demonstration | Show the real Amazon Location → Gateway → simulator → evidence path and actual tool/job receipts. |

These are expected benefits and evaluation methods, not measured results.

## Presenter rules

- Say “prerecorded radio report” and “assumed fire at the reported address.”
- Show historical year and current data time separately.
- Explain that traffic speeds are observed inputs while vehicles and demand are simulated.
- Let the actual result determine the story. Do not promise that coordinated or phased routing wins.
- Show both resident and crew outcomes before discussing congestion.
- If live data fails, show the failure. A saved run can be opened only with its original label and date.
- Frame a proposed change as an experiment until its result exists.
- Describe agent explanations as interpretations grounded in the available model evidence.

## Development priorities and evidence needed next

Priority 1 applies to this demonstration; priorities 2–6 describe what an organization would need before relying on a customer build.

1. **Make results understandable and auditable.** Keep the two objectives visible, show waiting and nonarrivals, use U.S. display units, preserve original artifacts, and reject invalid comparisons.
2. **Validate the roads and destinations.** Review lane counts, signals, turn restrictions, apparatus access, closures, staging locations, refuges and downstream bottlenecks with local specialists.
3. **Represent the people who must leave.** Add validated population and demand estimates, notification/preparation delays, mobility limitations, assisted evacuation and starting traffic. Do not treat equal vehicle occupancy as an equity assessment.
4. **Represent responder operations.** Introduce validated stations, apparatus, staffing, task priorities, access requirements and dispatch constraints. Fire-control effectiveness requires a separate task and fire-behavior model; the current two-minute synthetic stop cannot stand in for it.
5. **Test robustness.** Run matched scenarios across seeds, demand levels, hazard timing and growth, closures and incomplete data. Report variability and failure cases before generalizing.
6. **Validate with practitioners and incident evidence.** Compare with observed traffic and incident timelines, document limitations, and obtain the operational review appropriate to the proposed use before real-world decision support.

## Durable product principles

Every proposed feature should explain how it supports resident evacuation, responder access, or confidence in those decisions. Keep the benefit to one group and possible harm to the other visible. Preserve unresolved demand and missing crews in all accounting. Distinguish observations, assumptions, simulation outputs and validated conclusions.

Read this document when changing policy logic, metrics, traffic inputs, hazard behavior or result presentation. Update it explicitly if the intended business purpose changes. The [simulator contributor instructions](../traffic-simulator/AGENTS.md) and [README](../traffic-simulator/README.md) point here so future work starts from the same purpose.
