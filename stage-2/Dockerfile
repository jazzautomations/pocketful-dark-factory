# Pocketful stage 2 — standard-library Python service, no third-party dependencies.
FROM python:3.12.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
RUN python -m py_compile /app/app.py \
    && useradd --system --no-create-home pocketful
USER pocketful

EXPOSE 8080
CMD ["python", "/app/app.py"]
