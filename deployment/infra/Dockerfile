# CDK verification stage for the release build: type-check, unit-test and synthesize offline.
# Build from the repository root: docker build -f deployment/infra/Dockerfile .
# checkov:skip=CKV_DOCKER_2: build-only image; it runs the CDK build, tests and offline synth once and exits, so there is no running service to health-check (D13)
FROM public.ecr.aws/docker/library/node:22-bookworm-slim
# The worker-setting parity test reads Python AST only; it needs no worker packages or virtual environment.
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && python3 -c "import ast, json, pathlib" \
    && rm -rf /var/lib/apt/lists/*
# The build runs as the base image's unprivileged node user (CKV_DOCKER_3), in folders it owns.
RUN install -d -o node -g node /build/deployment/infra /build/schemas/simulation
USER node
WORKDIR /build/deployment/infra
COPY --chown=node:node deployment/infra/package.json deployment/infra/package-lock.json ./
# No install scripts run in the privileged build daemon. The lock's only install scripts are
# esbuild's binary check (its platform binary comes from the locked optional @esbuild/linux-x64
# package) and macOS-only fsevents, neither needed for build, test or synth.
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY --chown=node:node deployment/infra/ ./
COPY --chown=node:node schemas/simulation/capabilities.json /build/schemas/simulation/capabilities.json
# The logical-ID test reads the deploy guard's stateful types from its source.
COPY --chown=node:node deployment/change_sets.py /build/deployment/change_sets.py
# The AST helper resolves operational settings from the same captured worker source that builds its image.
COPY --chown=node:node traffic-simulator/traffic_simulator/*.py /build/traffic-simulator/traffic_simulator/
# The release-gate test verifies that each bind-mounted service test exists in the captured source.
COPY --chown=node:node firetruck-simulator/tests/ /build/firetruck-simulator/tests/
COPY --chown=node:node broadcastify/monitor/test_private.py broadcastify/monitor/test_radio_pipeline.py /build/broadcastify/monitor/
RUN npm run build && npm test && npm run synth
