# Build from the repository root; native calculator compilation is isolated from the non-root runtime.
ARG PYRO_WHEELS_IMAGE=pyro-native-artifact
FROM public.ecr.aws/docker/library/python:3.14-slim-trixie AS wheels-build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_BUILD_CONSTRAINT=/build/requirements-lock.txt
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake ninja-build \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements-lock.txt ./
COPY fire-risk-modeler/requirements.txt ./
RUN pip wheel --constraint requirements-lock.txt --requirement requirements.txt --wheel-dir /wheels

FROM scratch AS pyro-native-artifact
COPY --from=wheels-build /wheels/ /wheels/

FROM ${PYRO_WHEELS_IMAGE} AS wheels

FROM public.ecr.aws/docker/library/python:3.14-slim-trixie AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
       ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/app
COPY requirements-lock.txt ./
COPY fire-risk-modeler/requirements.txt ./fire-risk-modeler/
COPY --from=wheels /wheels/ /wheels/
RUN pip install --no-index --find-links /wheels --constraint requirements-lock.txt \
       --requirement fire-risk-modeler/requirements.txt && rm -rf /wheels
COPY deployment/settings.toml ./deployment/
COPY fire-risk-modeler/fire_risk_modeler/ ./fire-risk-modeler/fire_risk_modeler/
ENV PYTHONPATH=/opt/app/fire-risk-modeler

FROM base AS verification
COPY requirements-dev.txt pyproject.toml ./
COPY fire-risk-modeler/requirements-dev.txt ./fire-risk-modeler/
RUN pip install --constraint requirements-lock.txt --requirement fire-risk-modeler/requirements-dev.txt
COPY fire-risk-modeler/tests/ ./fire-risk-modeler/tests/
COPY fire-risk-modeler/scripts/ ./fire-risk-modeler/scripts/
RUN python -m pytest -q fire-risk-modeler/tests \
    && ruff check fire-risk-modeler/fire_risk_modeler

FROM base AS runtime
RUN groupadd -g 10001 modeler && useradd -u 10001 -g 10001 -M modeler \
    && chmod 1777 /tmp
VOLUME ["/tmp"]
USER 10001:10001
EXPOSE 8085
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/health', timeout=3)" || exit 1
CMD ["uvicorn", "fire_risk_modeler.service:app", "--host", "0.0.0.0", "--port", "8085"]
