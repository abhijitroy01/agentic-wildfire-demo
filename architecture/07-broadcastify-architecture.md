# Radio investigation

## Responsibility and boundaries

The deployed entry point is `broadcastify.monitor.private:app`. It exposes `GET /health` and a bearer-authenticated `/ws/monitor/start` WebSocket, forwarding only the permitted prerecorded-monitor operation. Public visitors connect through `backend/radio_broker.py`, which fixes the service destination and checks session ownership and stream limits.

| Module | Responsibility |
|---|---|
| `monitor/private.py`, `monitor/investigation.py` | Restrict the deployed surface, authenticate the broker and run the prerecorded investigation |
| `monitor/monitors/poc_monitor.py` | Read configured preserved S3 recordings and run the monitor |
| `monitor/audio_pipeline.py`, `transcription_client.py`, `keyword_detector.py` and `monitor/events/` | Transcription/extraction processing and event delivery |
| `shared/config.py` | Recording/provider configuration and operational limits |
| `backend/radio_broker.py` | Owned browser relay and private service connection |
| `backend/evacuation/locations.py` | Shared Amazon Location resolution after address extraction |

Finalized transcription windows matching a fire keyword without an exclusion pattern go to the Bedrock address extractor. Incomplete number-only locations wait for street context. The radio service emits address text; the broker resolves it using Amazon Location. Radio locations require an unambiguous California address/intersection and score at least 90. A recording does not imply current fire activity or live Broadcastify access.

The image builds FFmpeg 9.0.2 from checksum-pinned official source and verifies the
published signing identity. It retains MP3, WAV, MOV/MP4, Ogg and FLAC audio input,
PCM output and `pipe` I/O, with network protocols and video decoding disabled.
Only the built `ffmpeg`/`ffprobe` executables and license enter the runtime.
The image gate checks actual linkage/capabilities, valid decoding and rejected network
playlists; the live release gate additionally processes the configured preserved MP3.
When extending media support, review the allowed formats, build options, dependency
closure and both gates together. ECR basic scans do not inventory these source-built
native codecs; their pinned upstream versions need separate advisory review.

## Extension rules

The prerecorded investigation is the radio service's only operation. The local radio API and its report, chat, vocabulary and live-transcription services were removed on 2026-10-19. A new radio operation needs a reviewed broker operation, ownership checks, capacity limits and release acceptance before the UI offers it. Importing the deployed gate creates no AWS client, so the service starts with only its own settings and needs no AWS credential discovery.

## Verification

Run `.venv/bin/python -m pytest broadcastify/monitor` separately from the root suite. Existing tests protect the private route gate, credential redaction, transcription window behavior and extraction. Use [deployment operations](29-workspace-operations.md) for the private service image.
