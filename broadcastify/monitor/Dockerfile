FROM public.ecr.aws/docker/library/python:3.14-slim-trixie AS audio-build

# Only the fixed recording formats and PCM output are needed. A general-purpose
# distribution FFmpeg pulls in XML/image/network libraries that this service never uses.
# The pinned official release is also checked against the published signing identity.
# checkov:skip=CKV_DOCKER_4: ADD --checksum verifies pinned official source/signature/key bytes before the build uses them
ADD --checksum=sha256:8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e \
    https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz /tmp/ffmpeg.tar.xz
ADD --checksum=sha256:d617fd94ea354dadd2a8bb16243d37c44e1e9729b06b6ad58fe30bfb0fa943f2 \
    https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz.asc /tmp/ffmpeg.tar.xz.asc
ADD --checksum=sha256:397b3becedcd5a98769967ff1ff8501ddc89f8368b8f766e4701377d7dbaabe5 \
    https://ffmpeg.org/ffmpeg-devel.asc /tmp/ffmpeg-key.asc
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends gcc libc6-dev make xz-utils gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && gpg --batch --import /tmp/ffmpeg-key.asc \
    && gpg --batch --assert-signer FCF986EA15E6E293A5644F10B4322F04D67658D8 \
        --verify /tmp/ffmpeg.tar.xz.asc /tmp/ffmpeg.tar.xz \
    && mkdir /build && tar -xJf /tmp/ffmpeg.tar.xz -C /build --strip-components=1
WORKDIR /build
RUN ./configure --prefix=/opt/ffmpeg --disable-everything --disable-autodetect \
    --disable-doc --disable-debug --disable-network --disable-x86asm --enable-small \
    --enable-ffmpeg --enable-ffprobe --disable-avdevice --disable-swscale \
    --enable-protocol=pipe --enable-demuxer=mp3,wav,mov,ogg,flac \
    --enable-decoder='mp3,mp3float,aac,aac_fixed,alac,flac,vorbis,opus,pcm_*,adpcm_*' \
    --enable-parser=mpegaudio,aac,flac,vorbis,opus \
    --enable-encoder=pcm_s16le --enable-muxer=pcm_s16le \
    --enable-filter=abuffer,abuffersink,aresample,aformat,anull \
    && make -j4 && make install \
    && mkdir -p /opt/ffmpeg/share/licenses \
    && cp COPYING.LGPLv2.1 /opt/ffmpeg/share/licenses/ \
    && /opt/ffmpeg/bin/ffmpeg -version

FROM public.ecr.aws/docker/library/python:3.14-slim-trixie

# Set shell with pipefail to catch errors in piped commands
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get upgrade -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY --from=audio-build /opt/ffmpeg/bin/ /opt/ffmpeg/bin/
COPY --from=audio-build /opt/ffmpeg/share/licenses/ /opt/ffmpeg/share/licenses/
ENV PATH="/opt/ffmpeg/bin:$PATH"
LABEL org.opencontainers.image.ffmpeg.version="9.0.2" \
    org.opencontainers.image.ffmpeg.source="https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz" \
    org.opencontainers.image.ffmpeg.sha256="8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"

# Set working directory to broadcastify package root
WORKDIR /app/broadcastify

# Copy package __init__.py files first (establish package structure)
COPY __init__.py ./

# Copy shared module (configuration, AWS clients, logging)
COPY shared/ ./shared/
COPY requirements-lock.txt ./

# Copy and install Python dependencies
COPY monitor/requirements.txt ./monitor/
RUN pip install --no-cache-dir --constraint requirements-lock.txt -r monitor/requirements.txt

# Copy monitor application code
COPY monitor/ ./monitor/

# Create non-root user for security (CKV_DOCKER_3)
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser && \
    chown -R appuser:appuser /app
USER appuser

# Set PYTHONPATH so the broadcastify package can be imported
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Set working directory to monitor for running the app
WORKDIR /app/broadcastify/monitor

# Healthcheck (CKV_DOCKER_2)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run the deployed private gate (private.py). The container binds every interface here,
# and only the workspace API task can reach it.
# Access logs stay on: the gate takes its key from the Authorization header, never the URL,
# and api_key query values are redacted from uvicorn log records (shared/logger.py).
CMD ["uvicorn", "broadcastify.monitor.private:app", "--host", "0.0.0.0", "--port", "8000"]
