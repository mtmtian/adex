FROM debian:bookworm-slim AS whisper-build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# whisper.cpp v1.7.6. Pin the source and avoid build-host-specific instructions.
ARG WHISPER_COMMIT=a8d002cfd879315632a579e73f0148d06959de36
RUN git init whisper && cd whisper \
    && git remote add origin https://github.com/ggml-org/whisper.cpp.git \
    && git fetch --depth 1 origin "$WHISPER_COMMIT" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$WHISPER_COMMIT" \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
       -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF \
       -DGGML_AVX=OFF -DGGML_AVX2=OFF -DGGML_FMA=OFF -DGGML_F16C=OFF \
       -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF \
    && cmake --build build --target whisper-cli --parallel 2

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 ffmpeg tesseract-ocr tesseract-ocr-eng ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid worker --no-create-home worker
COPY --from=whisper-build /build/whisper/build/bin/whisper-cli /usr/local/bin/whisper-cli
WORKDIR /app
COPY *.py ./
COPY tests ./tests
COPY fixtures ./fixtures
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    WORKER_ENABLE_EXECUTION=0 REMIX_MAX_TOKENS=0 \
    WHISPER_MODEL=/models/ggml-model.bin
RUN whisper-cli --help && ffmpeg -version && tesseract --version
USER 10001:10001
ENTRYPOINT ["python3", "worker.py"]
CMD ["--job-file", "fixtures/job.json"]

# Production adds a public model at build time, never on a job's paid path.
# The URL should be an immutable Hugging Face revision or approved artifact URL.
FROM debian:bookworm-slim AS model
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG WHISPER_MODEL_URL
ARG WHISPER_MODEL_SHA256
RUN test -n "$WHISPER_MODEL_URL" && test -n "$WHISPER_MODEL_SHA256" \
    && mkdir /models \
    && curl --fail --location --proto '=https' --proto-redir '=https' \
       --max-time 600 "$WHISPER_MODEL_URL" -o /models/ggml-model.bin \
    && echo "$WHISPER_MODEL_SHA256  /models/ggml-model.bin" | sha256sum --check -

FROM runtime AS production
COPY --from=model /models/ggml-model.bin /models/ggml-model.bin
RUN python3 -c "import media; media.validate_runtime()"
RUN python3 -c "import media, tempfile; from pathlib import Path; d = tempfile.TemporaryDirectory(); p = Path(d.name); media.placeholder(p / 'test.mp4', '1:1', 1); result = media.qc_scan(p / 'test.mp4', p / 'qc'); assert result['completed'], result['checks']; d.cleanup()"
