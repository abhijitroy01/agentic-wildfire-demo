# FireGuard AI demo scope

- FireGuard AI is a demonstration of what agentic systems can do, not a production or operational system.
- A customer that wants to build its own agentic system from this idea could map its vehicle telemetry and GIS assets into it, so its trucks and crews can maintain those assets. Present that in business-case and narrative text as a possible customer build. Do not design or build telemetry ingestion, GIS integration or IoT services in the demo.
- Keep simulation-accuracy analysis brief. Label synthetic and illustrative values plainly, as `traffic-simulator/AGENTS.md` requires, and skip deep analysis of vehicle physics, fleet specifications or supply rates unless a demo viewer would notice the problem.
- Direct mode (the Radio Monitor console, the camera direct client, the firetruck direct API and the older radio broker fallback) was removed on 2026-10-19; the UI reaches the agent, radio, firetruck and camera services only through the broker, published builds stay broker-only, and nothing published carries a provider key.
- Demo scope never relaxes the existing public-demo security controls or engineering standards. Every change follows software best practices for maintainability: reuse or refactor existing code before adding new code, remove the code a change replaces, and add tests that can fail meaningfully.
