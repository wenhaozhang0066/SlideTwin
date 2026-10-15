# SlideTwin image for a Hugging Face Space (docker SDK) or a local machine.
# Research prototype, not for clinical use. Slides are mounted or downloaded at runtime,
# never baked into the image.
FROM python:3.12-slim

ARG GIT_SHA=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SLIDETWIN_GIT_SHA=${GIT_SHA} \
    SLIDETWIN_CONFIG_DIR=/app/configs \
    SLIDETWIN_SLIDE_DIR=/data/slides \
    SLIDETWIN_LOG_DIR=/tmp/slidetwin/logs \
    SLIDETWIN_CACHE_DIR=/tmp/slidetwin/cache

RUN useradd --create-home --uid 1000 user
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY slidetwin ./slidetwin
COPY configs ./configs
RUN pip install .

USER user
EXPOSE 7860
# One worker: sessions live in process memory.
CMD ["uvicorn", "slidetwin.api.main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
