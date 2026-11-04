/* The accessible names, visible texts and test ids that the release browser harness and the manual post-deploy checks
 * (deployment/acceptance/check_public_sop_browser.cjs, check_prepared_simulation_browser.cjs) both query. The harness
 * asserts every one against the public-demo build before any AWS change, so a UI rename fails the deploy gate and is
 * fixed here once for both. No dependencies: the manual checks require this file by its path from theirs. */

module.exports = Object.freeze({
  // The header's numbered workflow steps.
  STAGE_1: "1 · Investigate",
  STAGE_2: "2 · Evacuation & Crew Access",
  STAGE_3: "3 · Response Tracker",
  // 1 · Investigate chat.
  CHAT_INPUT: "Type your message",
  SEND_MESSAGE: "Send message",
  AGENT_MESSAGE: "Message from FireGuard AI",
  PROCESSING: "Processing your request",
  INSPECT_EVIDENCE: "Inspect this answer’s evidence",
  // 2 · Evacuation & Crew Access: the radio tab's typed-address entry and the prepared draft's review.
  RADIO_TAB: "Radio Investigation",
  ADDRESS_ENTRY: "Enter or correct a reported address",
  ADDRESS_INPUT: "Reported address",
  RESOLVE_AND_PREPARE: "Resolve & prepare",
  PREPARING: "Preparing road network",
  REVIEW_DRAFT: "Review scenario draft",
  RUN_DRAFT: "Run this draft",
  SMALLER_AREA: "Smaller area proposed:",
  // The guided replay and its views.
  GUIDED_TEST_ID: "guided-scenario",
  GUIDED_HEADLINE: "People out. Crews in. Check both outcomes.",
  SIMULATOR_BUSY: "Working with simulator tools",
  COORDINATED: "Coordinated",
  FIRE_CREWS: "Fire crews",
  CREW_ROUTES: "Show planned crew routes, origins and staging points",
  CREW_TRAILS: "Show recorded crew trails in orange",
  PLAYBACK_SPEED: "Playback speed",
  ENLARGE_MAP: "Enlarge map",
  RESTORE_MAP: "Restore map",
  DATA_EVIDENCE: "Data & evidence",
  ASSUMPTIONS: "Scenario assumptions",
});
