# Architecture documentation

Start with the [decision log](decisions.md), [Current system](00-current-system.md), [Developer setup](12-developer-setup.md) and [Workspace operations](29-workspace-operations.md). API contracts and generated schemas are authoritative for payload shape; prose describes intent and boundaries. The guides are grouped by [Diátaxis](https://diataxis.fr/) type.

| Guide | Type | Responsibility |
|---|---|---|
| [Decision log](decisions.md) | Decision record | The decision IDs that code, tests and docs cite (such as D23 or G17): decision, reason, date and status |
| [Current FireGUard implementation](00-current-system.md) | Explanation | Implemented capabilities, component authority, known limits and backlog |
| [Agent runtime, tools and providers](02-multi-agent-orchestration.md) | Explanation and reference | Agent composition, Gateway and provider adapters, prompts and grounding, SDK notes |
| [Data flow, ownership and memory](04-data-flow-and-session-management.md) | Explanation | Ownership, signed context, Memory, completion and retention |
| [Radio investigation](07-broadcastify-architecture.md) | Explanation | Private radio workflow |
| [Firetruck Response Tracker](09-firetruck-simulation-spec.md) | Explanation | Synthetic suppression, durable evidence and reports |
| [UI architecture](10-ui-architecture.md) | Explanation | Feature boundaries, broker transport, workflow and browser acceptance IDs |
| [Model and implementation](32-traffic-simulator-architecture.md) | Explanation | Network, background road preparation, policies, sources and engine behavior |
| [Business justification: evacuation and fire-crew access](33-evacuation-crew-access-business-justification.md) | Explanation | Authoritative business case, data meaning, presenter rules and outcome interpretation |
| [Public demonstration access](36-public-demo-security.md) | Explanation | Public trust boundaries, controls, supply chain and image gate |
| [Developer setup](12-developer-setup.md) | How-to | Local setup and component verification |
| [FireGUard deployment and operations](29-workspace-operations.md) | How-to with generated reference | Installation, deployment, post-deployment tests and the settings table |
| [45. Credential rotation and revocation runbook](45-credential-rotation-runbook.md) | How-to | Credential replacement and revocation |
| [Public SOP guidance collection](28-public-sop-collection.md) | Reference and how-to | Reviewed public guidance and corpus maintenance |
| [Simulator research and source register](44-traffic-simulator-research-notes.md) | Reference | Model, source and provider references used by future extensions |
| [Wildfire risk map assessment](wildfire-risk-model-assessment.md) | Explanation and research | Current distance heuristics, review findings, fire spread model alternatives and proposed map layers; assessment only |
| [Wildfire exposure demo plan](wildfire-risk-demo-plan.md) | Proposed implementation plan and source review | Wind-informed exposure at any valid reported fire coordinates using Esri/DTN; optional free timed scenarios and geographic context, with conditional simulator integration notes |
| [Wildfire risk: final refactor and implementation proposal](wildfire-risk-implementation-proposal.md) | Approved source refactor and feature delivery | Approved scope implemented in source: capability packages directly under backend, thin app entrypoint, independent private ECS modeler, saved exposure and timed scenarios; the fire exposure guide describes the result |
| [Fire exposure implementation](48-fire-exposure-implementation.md) | Implementation guide | Capability ownership, source/model flow, map interpretation and the terrain and road design corrections |
| [U.S. scenarios and build efficiency](49-us-scenarios-and-build-efficiency.md) | Implementation guide | Nationwide scenario preparation, exact reported-fire handoff, affected-component reuse, native artifacts and ordered concurrent builds |
| [Earlier Incident Coordination API proposal](incident-coordination-api-refactor-proposal.md) | Superseded proposal and audit evidence | Historical API naming/layout recommendation; replaced by the consolidated wildfire risk proposal |

Module docs: the [repository README](../README.md), the [deployer](../deployment/README.md), the [workspace API](../backend/README.md), the [traffic simulator](../traffic-simulator/README.md) and its [contributor rules](../traffic-simulator/AGENTS.md), the [public guidance collection](../backend/knowledge/public-sop/README.md) and the [demo scope steering](../.kiro/steering/demo-scope.md). Original recordings, media, reviewed guidance and source snapshots, and installation receipts are retained.
