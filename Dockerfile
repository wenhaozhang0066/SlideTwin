# SlideTwin image for a Hugging Face Space (docker SDK) or a local machine.
# Research prototype, not for clinical use. Slides are never baked into the image: they are
# downloaded at start (the demo's public slides, configs/slides/demo.yaml, 0.9 GB, sha256-checked)
# or mounted at /data/slides.
#
#   docker build -t slidetwin --build-arg GIT_SHA=$(git rev-parse --short HEAD) .
#   docker run -p 7860:7860 slidetwin                                   # fetch the demo slides
#   docker run -p 7860:7860 -e SLIDETWIN_FETCH_SLIDES=0 \
#     -v /path/to/slides:/data/slides -v /path/to/manifest.yaml:/data/manifest.yaml \
#     -e SLIDETWIN_MANIFEST=/data/manifest.yaml slidetwin              # your own slides
FROM python:3.12-slim

ARG GIT_SHA=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SLIDETWIN_GIT_SHA=${GIT_SHA} \
    SLIDETWIN_CONFIG_DIR=/app/configs \
    SLIDETWIN_MANIFEST=/app/configs/slides/demo.yaml \
    SLIDETWIN_SLIDE_DIR=/data/slides \
    SLIDETWIN_LOG_DIR=/tmp/slidetwin/logs \
    SLIDETWIN_CACHE_DIR=/tmp/slidetwin/cache \
    SLIDETWIN_FETCH_SLIDES=1 \
    SLIDETWIN_RATE_LIMIT_PER_S=20 \
    SLIDETWIN_RATE_LIMIT_BURST=60 \
    SLIDETWIN_RATE_LIMIT_CLIENT_HEADER=x-forwarded-for \
    FORWARDED_ALLOW_IPS=*

RUN useradd --create-home --uid 1000 user && mkdir -p /data/slides && chown -R user /data
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY slidetwin ./slidetwin
COPY configs ./configs
# COPY keeps the build context's file modes; make the app readable whatever the builder's umask.
RUN chmod -R a+rX /app && pip install .

USER user
EXPOSE 7860
# One worker: sessions live in process memory.
CMD ["python", "-m", "slidetwin.serve"]
