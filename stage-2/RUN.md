# Pocketful — stage 2

Build and start the service (listens on `0.0.0.0:$PORT`, default 8080):

```sh
docker build -t pocketful-stage-2 . && docker run --rm -e PORT=8080 -p 8080:8080 pocketful-stage-2
```

Health check: `curl http://localhost:8080/health` returns `{"status": "ok"}`.

All state is in memory; seed it with `POST /_test/reset`. The service uses only the
Python standard library, so no packages are downloaded at build or run time beyond the
pinned base image `python:3.12.7-slim-bookworm` (multi-architecture).

The browser UI is served by the same process: open `http://localhost:8080/login`.
Its HTML, CSS and JavaScript live in `static/` and are copied into the image; no fonts,
scripts or stylesheets are fetched from the network.
