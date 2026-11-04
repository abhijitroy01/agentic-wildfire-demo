# Traffic simulator: product context

Read the [business justification](../architecture/33-evacuation-crew-access-business-justification.md) before changing policy behavior, metrics, source inputs or result presentation, here, in `backend/`, or in the UI's evacuation and Traffic Lab views. It records the central purpose: help residents leave wildfire areas efficiently while helping fire crews reach the incident efficiently and with less exposure.

Evaluate civilian evacuation and crew access together. Show tradeoffs, waiting, exposure, unresolved demand and crews that never arrive. Do not declare success from a smaller queue, a faster average among only successful arrivals, or an unexplained combined score.

Keep observed data, scenario assumptions and simulated outcomes distinct. Model exits are not verified safety, staging is not fire suppression, and exposure is not an injury or fatality estimate. Missing results must remain unavailable. Use U.S. units in the interface while retaining documented SI units in APIs, SUMO inputs and saved artifacts.

Preserve existing recordings and unrelated workspace work. See [README.md](README.md), the [simulator architecture](../architecture/32-traffic-simulator-architecture.md) and the [known limits and backlog](../architecture/00-current-system.md#known-limits-and-backlog) for implementation and verification context.
