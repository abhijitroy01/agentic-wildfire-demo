# Traffic simulator worker: headless SUMO built from a checksum-pinned source archive.
# No syntax directive: the daemon's built-in Dockerfile frontend is used, so no frontend image is
# pulled from Docker Hub during the release build.
# Build from the repository root: docker build -f traffic-simulator/Dockerfile --target runtime .

ARG SUMO_NATIVE_IMAGE=sumo-native-artifact
FROM public.ecr.aws/docker/library/python:3.14-slim-trixie AS sumo-build
ARG SUMO_VERSION=1.27.1
# Annotated upstream v1_27_1 tag resolves to this commit. Verify the archive bytes.
# checkov:skip=CKV_DOCKER_4: ADD --checksum verifies the remote archive's SHA-256 before the build uses it; COPY cannot fetch a URL, and ADD here fetches only this pinned archive (D13)
ADD --checksum=sha256:a6c233c6122578d5bf431ded42838c0295517b33941d82408a5b8820b55d74ac \
    https://codeload.github.com/eclipse-sumo/sumo/tar.gz/7717f2379d9e314a0c81c5cec748444de06a2a91 /tmp/sumo.tar.gz
ADD --checksum=sha256:a83e12af82dc4fea09c592979fdbb6f206eeaa968562d7d18a0a4dd032c51267 \
    https://dlcdn.apache.org/xerces/c/3/sources/xerces-c-3.3.0.tar.xz /tmp/xerces.tar.xz
ADD --checksum=sha256:791a0610547eeabb17006cfd49cdbd2034f3240f47ed5e88a1031811f4e2bcf3 \
    https://github.com/OSGeo/PROJ/releases/download/9.9.0/proj-9.9.0.tar.gz /tmp/proj.tar.gz
ADD --checksum=sha256:e629d2f3e500ffa5df6f1b1689161ab3dea3a82f66beec2b453a74b8d782f949 \
    https://codeload.github.com/osmcode/osmium-tool/tar.gz/refs/tags/v1.19.1 /tmp/osmium.tar.gz
ADD --checksum=sha256:eb47d8163396c73870dbf27819dcadda0c56f0191935c4f8659d1e61b261552f \
    https://codeload.github.com/osmcode/libosmium/tar.gz/refs/tags/v2.23.1 /tmp/libosmium.tar.gz
ADD --checksum=sha256:6c7a896f1dc08435e8cd4f3780ff688cd0bfce6890599b755f6f3cb36398dc25 \
    https://codeload.github.com/mapbox/protozero/tar.gz/refs/tags/v1.8.1 /tmp/protozero.tar.gz
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    cmake g++ make libsqlite3-dev sqlite3 zlib1g-dev xz-utils \
    libboost-program-options-dev libexpat1-dev libbz2-dev liblz4-dev nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /build/sumo /build/xerces /build/proj /build/osmium /build/libosmium /build/protozero \
    && tar -xzf /tmp/sumo.tar.gz -C /build/sumo --strip-components=1 \
    && tar -xJf /tmp/xerces.tar.xz -C /build/xerces --strip-components=1 \
    && tar -xzf /tmp/proj.tar.gz -C /build/proj --strip-components=1 \
    && tar -xzf /tmp/osmium.tar.gz -C /build/osmium --strip-components=1 \
    && tar -xzf /tmp/libosmium.tar.gz -C /build/libosmium --strip-components=1 \
    && tar -xzf /tmp/protozero.tar.gz -C /build/protozero --strip-components=1
# SUMO reads local server-generated files and uses UTM projection. Native URL access,
# online grid downloads and TIFF grids are unnecessary; provider HTTP stays in Python.
RUN cmake -S /build/xerces -B /build/xerces-build -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/sumo -DCMAKE_INSTALL_LIBDIR=lib \
    -Dnetwork=OFF -Dtranscoder=gnuiconv -DBUILD_TESTING=OFF \
    && cmake --build /build/xerces-build --parallel 4 \
    && cmake --install /build/xerces-build \
    && cmake -S /build/proj -B /build/proj-build -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/sumo -DCMAKE_INSTALL_LIBDIR=lib \
        -DENABLE_CURL=OFF -DENABLE_TIFF=OFF -DBUILD_TESTING=OFF -DBUILD_APPS=OFF \
    && cmake --build /build/proj-build --parallel 4 \
    && cmake --install /build/proj-build \
    && mkdir -p /opt/sumo/share/licenses \
    && cp /build/xerces/LICENSE /opt/sumo/share/licenses/xerces.txt \
    && cp /build/proj/COPYING /opt/sumo/share/licenses/proj.txt
RUN cmake -S /build/sumo -B /build/sumo-build -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH=/opt/sumo -DCMAKE_BUILD_RPATH=/opt/sumo/lib \
    -DCHECK_OPTIONAL_LIBS=OFF -DENABLE_FOX=OFF -DENABLE_GDAL=OFF \
    -DENABLE_FFMPEG=OFF -DENABLE_OSG=OFF -DENABLE_GL2PS=OFF \
    -DENABLE_JUPEDSIM=OFF -DENABLE_BOOST=OFF -DENABLE_PARQUET=OFF \
    -DENABLE_NETEDIT=OFF -DENABLE_FMI=OFF -DENABLE_EIGEN=OFF \
    -DENABLE_FMT=OFF -DENABLE_GTEST=OFF -DENABLE_TCMALLOC=OFF \
    -DENABLE_PYTHON_BINDINGS=OFF -DENABLE_JAVA_BINDINGS=OFF -DENABLE_CS_BINDINGS=OFF \
    && cmake --build /build/sumo-build --target sumo netconvert --parallel 4 \
    && /build/sumo/bin/sumo --version | grep -F "$SUMO_VERSION" \
    && /build/sumo/bin/netconvert --version | grep -F "$SUMO_VERSION"
# The complete-way PBF reader is a CLI: no Python-version-specific native wheel.
RUN cmake -S /build/osmium -B /build/osmium-build -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/sumo -DOSMIUM_INCLUDE_DIR=/build/libosmium/include \
    -DPROTOZERO_INCLUDE_DIR=/build/protozero/include -DBUILD_TESTING=OFF \
    && cmake --build /build/osmium-build --target osmium --parallel 4 \
    && install -m 755 /build/osmium-build/src/osmium /opt/sumo/bin/osmium \
    && /opt/sumo/bin/osmium --version | grep -F "osmium version 1.19.1" \
    && cp /usr/lib/*-linux-gnu/libboost_program_options.so* /opt/sumo/lib/ \
    && cp /build/osmium/LICENSE.txt /opt/sumo/share/licenses/osmium-tool.txt \
    && cp /build/libosmium/LICENSE /opt/sumo/share/licenses/libosmium.txt \
    && cp /build/protozero/LICENSE.md /opt/sumo/share/licenses/protozero.txt \
    && cp /build/protozero/LICENSE.from_folly /opt/sumo/share/licenses/protozero-folly.txt \
    && cp /usr/share/doc/libboost-program-options-dev/copyright /opt/sumo/share/licenses/boost.txt

FROM scratch AS sumo-native-artifact
# Only the compiled engine, pinned libraries, projection data and licenses enter the reusable artifact.
COPY --from=sumo-build /build/sumo/bin/sumo /build/sumo/bin/netconvert /opt/sumo/bin/
COPY --from=sumo-build /opt/sumo/bin/osmium /opt/sumo/bin/
COPY --from=sumo-build /build/sumo/data/ /opt/sumo/data/
COPY --from=sumo-build /build/sumo/LICENSE /build/sumo/NOTICE.md /opt/sumo/
COPY --from=sumo-build /opt/sumo/lib/*.so* /opt/sumo/lib/
COPY --from=sumo-build /opt/sumo/share/proj/ /opt/sumo/share/proj/
COPY --from=sumo-build /opt/sumo/share/licenses/ /opt/sumo/share/licenses/

FROM ${SUMO_NATIVE_IMAGE} AS sumo-native

FROM public.ecr.aws/docker/library/python:3.14-slim-trixie AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    libgomp1 libstdc++6 libsqlite3-0 zlib1g ca-certificates libexpat1 libbz2-1.0 liblz4-1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=sumo-native /opt/sumo/ /opt/sumo/
RUN echo /opt/sumo/lib > /etc/ld.so.conf.d/fireguard-sumo.conf && ldconfig
LABEL org.opencontainers.image.proj.version="9.9.0" \
    org.opencontainers.image.proj.sha256="791a0610547eeabb17006cfd49cdbd2034f3240f47ed5e88a1031811f4e2bcf3" \
    org.opencontainers.image.xerces.version="3.3.0" \
    org.opencontainers.image.xerces.sha256="a83e12af82dc4fea09c592979fdbb6f206eeaa968562d7d18a0a4dd032c51267" \
    org.opencontainers.image.osmium.version="1.19.1" \
    org.opencontainers.image.osmium.sha256="e629d2f3e500ffa5df6f1b1689161ab3dea3a82f66beec2b453a74b8d782f949"
WORKDIR /opt/app
COPY requirements-lock.txt ./
COPY traffic-simulator/requirements.txt ./traffic-simulator/
# The repository lock pins every transitive dependency; the SUMO wheel is never installed here.
RUN pip install --constraint requirements-lock.txt --requirement traffic-simulator/requirements.txt
ENV SUMO_HOME=/opt/sumo PATH="/opt/sumo/bin:$PATH" PYTHONPATH=/opt/app/traffic-simulator
# The runtime gate reads the reviewed eclipse-sumo pin (requirements-sumo.txt, not installed) and
# checks that the source-built binaries match it.
COPY traffic-simulator/requirements-sumo.txt ./traffic-simulator/
COPY traffic-simulator/scripts/check_sumo_runtime.py ./traffic-simulator/scripts/
COPY traffic-simulator/scripts/check_osmium_runtime.py ./traffic-simulator/scripts/
COPY traffic-simulator/scripts/benchmark_osm.py ./traffic-simulator/scripts/
RUN python traffic-simulator/scripts/check_sumo_runtime.py \
    && python traffic-simulator/scripts/check_osmium_runtime.py
# Operational settings: traffic_simulator.settings checks every limit variable against its declared range.
COPY deployment/settings.toml ./deployment/
COPY traffic-simulator/traffic_simulator/ ./traffic-simulator/traffic_simulator/

# Inherits the SUMO runtime gate that already ran in base; only test and lint tools are added.
FROM base AS verification
COPY requirements-dev.txt pyproject.toml ./
RUN pip install --constraint requirements-lock.txt --requirement requirements-dev.txt
COPY traffic-simulator/tests/ ./traffic-simulator/tests/
COPY traffic-simulator/scripts/ ./traffic-simulator/scripts/
RUN python -m pytest -q traffic-simulator/tests \
    && ruff check traffic-simulator

FROM base AS runtime
RUN groupadd -g 10001 simulator && useradd -u 10001 -g 10001 -M simulator \
    && chmod 1777 /tmp && mkdir /tmp/wildfire && chown 10001:10001 /tmp/wildfire
VOLUME ["/tmp"]
ENV DATA_DIR=/tmp/wildfire
USER 10001:10001
# checkov:skip=CKV_DOCKER_2: the SUMO worker is a queue consumer with no listener to probe; ECS restarts it when the process exits, and the dead-letter alarm reports runs it cannot finish (D13)
CMD ["python", "-m", "traffic_simulator.worker"]
