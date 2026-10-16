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
#
# Readers: the base install, OpenSlide and iSyntax. OpenSlide is LGPL-2.1, so the image carries the
# source of exactly the build it ships (the openslide-bin release archive holds OpenSlide and every
# library it bundles, with their licences). slideio (VSI, ZVI) is left out until the notices of the
# libraries it bundles are collected. Licences: /usr/share/slidetwin/THIRD_PARTY_NOTICES.txt.
FROM python:3.12-slim

ARG GIT_SHA=unknown
ARG OPENSLIDE_BIN=4.0.1.2
ARG OPENSLIDE_BIN_SHA256=83c9a4a598f3a5fe1dea83e123745e48b94ce7eab99833f04902bdf4fa0af1db
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
COPY third_party ./third_party
COPY scripts/third_party_notices.py scripts/fetch_checked.py ./scripts/
# COPY keeps the build context's file modes; make the app readable whatever the builder's umask.
RUN chmod -R a+rX /app \
 && pip install ".[openslide,isyntax]" "openslide-bin==${OPENSLIDE_BIN}" \
 && mkdir -p /usr/share/slidetwin/sources \
 && python scripts/fetch_checked.py \
    "https://github.com/openslide/openslide-bin/releases/download/v${OPENSLIDE_BIN}/openslide-bin-${OPENSLIDE_BIN}.tar.gz" \
    "${OPENSLIDE_BIN_SHA256}" "/usr/share/slidetwin/sources/openslide-bin-${OPENSLIDE_BIN}.tar.gz" \
 && python scripts/third_party_notices.py --out /usr/share/slidetwin/THIRD_PARTY_NOTICES.txt \
 && chmod -R a+rX /usr/share/slidetwin

USER user
EXPOSE 7860
# One worker: sessions live in process memory.
CMD ["python", "-m", "slidetwin.serve"]
