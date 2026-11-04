# Data flow, ownership and memory

## Responsibility and boundaries

The browser uses the same-origin workspace broker. Its opaque Secure/HttpOnly/SameSite cookie identifies a visitor; browser state holds references and view selection, never authorization or authoritative results. Every read, change, stream and report is checked against its owner.

| State | Owner and storage |
|---|---|
| Demo identity | `backend/demo_access.py`; server-side session records |
| Session, incident, draft and turn revisions | `backend/evacuation/service.py` and broker modules; conditional durable records |
| Job admission and delivery | Transactional idempotency/admission/outbox in `traffic_simulator/persistence.py`; SQS and dispatcher |
| Execution progress and completion | `traffic_simulator/worker.py` and `store.py`; attempt-fenced leases and immutable S3 artifacts |
| Frozen input bundle and evidence | `traffic_simulator/input_bundles.py` and `backend/evacuation/evidence.py` |
| Firetruck execution/evidence | `firetruck-simulator/api/private.py`; DynamoDB owner/attempt/lease and S3 snapshots |
| Conversation continuity | AgentCore short-term Memory, separate from durable simulation artifacts |
| Investigation source observations | General Investigation broker; bounded owner/conversation metadata receipts from successful completed turns, with existing session TTL |

## Signed context and continuity

The broker signs purpose-specific contexts for investigation, simulation and firetruck reports. The Runtime verifies those contexts using `utils/simulation_context.py`; unsigned identity fields never select a Memory actor. Investigation memory identity comes from the signed investigation context bound to the invocation thread. Simulation actors use the signed subject. Invalid present tokens are rejected. Unsigned investigation requests receive no broker-owned Memory identity.

Supervisor and simulation turns use `AgentCoreMemorySessionManager` when a verified identity is available. The installation provisions short-term events with 30-day expiry, no long-term strategies and the default AWS-owned key. Firetruck reports use no Memory. General investigation stores the user question separately from reference data; older turns may contain UI envelopes, which projection strips when extracting prior questions. Public guidance retrieval never searches private Memory, and the Runtime keeps no retrieval cache.

General Investigation restores up to 40 native messages through the same verified session manager before selecting tasks; a bounded projection keeps eight prior requests and up to eight successful detection receipts within 64 KiB, with provider locations and timestamps. No additional conversation database or long-term Memory strategy is used. The broker's retained final snapshots are a separate server-owned source of previous observations; [topic routing](02-multi-agent-orchestration.md#investigation-topic-routing) describes how both reach the specialists.

The broker invokes an explicit Runtime endpoint, `RELEASE_A` or `RELEASE_B`, and derives the execution-session UUID from the Runtime ARN, numeric version and logical conversation, so a release changes the executing code while the signed actor, AG-UI thread and Memory session stay the same. An incomplete endpoint or version configuration fails before invocation. A failed promotion restores the prior binding but does not erase newer Memory messages; [workspace operations](29-workspace-operations.md#what-the-deployment-does) describes the cutover.

Browser chat retains each completed answer's typed evidence for its details action.
Selecting an earlier answer restores that answer's source location, citations,
current weather, forecast and recordings in the shared drawer. The latest answer
does not replace earlier evidence. This browser view state provides no additional
Memory or authorization. Explicit communication receives one verified selected
incident and its matching source observations. Translation/speech/storage run
through bounded code helpers; private S3 object keys derive from the signed actor
and conversation plus a unique message ID. Playback URLs expire after one hour.
Review text remains visible after expiry; no notification is distributed.

## Completion and recovery

A successful agent response requires a valid state snapshot and a successful terminal event. Partial prose or a disconnected stream is not a completed answer. `ui/src/shared/services/AgentEventStream.ts` implements this rule for investigation and simulation. Simulation additionally validates the saved response against its requested session, incident and location revision.

Restoration reuses owned saved run evidence and analysis, preserving selected policy and replay time without duplicate submission. Idempotency keys distinguish replaying an operation from requesting a new experiment. Worker publication requires the live attempt's lease; stale workers cannot overwrite completed artifacts.

Memory expiry, metadata TTL and object retention are distinct policies. Submission and variant creation extend referenced metadata, including the parent chain, monotonically through the new lifetime. Extensions increment revisions so older transactions cannot shorten retention. An hourly collector deletes only runs/reports whose authoritative metadata is absent and whose objects are at least a day old; it rechecks metadata before deletion and checkpoints each bounded page. Noncurrent object versions retain the existing 30-day lifecycle. The legacy deployment's recordings, runtime sources and knowledge-base documents stay in retained buckets, and its Memory, knowledge bases and KMS keys are deleted (the keys on 2026-10-25, D55).

## Verification

API tests cover ownership, revision conflicts, idempotent replay, signed context and persisted analysis. Worker tests cover cancellation, duplicate delivery, lease loss and publication fencing. Runtime and UI completion checks are listed in [developer setup](12-developer-setup.md).
