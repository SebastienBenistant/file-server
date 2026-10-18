# Python File Server

A small FastAPI application that serves a browser UI and API for browsing and managing files within configured directories.

## Requirements

- Python 3.10 or newer
- Access to the directories you configure

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Configure and run

Set `FILE_SERVER_ROOTS` to a JSON object mapping root IDs to absolute paths. Root IDs contain letters, digits, `_`, or `-` and start with a letter or digit.

```powershell
$env:FILE_SERVER_ROOTS = '{"documents":"C:\\Users\\me\\Documents","shared":"D:\\Shared"}'
$env:FILE_SERVER_TOKEN = 'replace-with-a-long-random-token'
$env:FILE_SERVER_MAX_UPLOAD_BYTES = '104857600'
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. `FILE_SERVER_TOKEN` is optional; without it, no authentication is required. The UI stores the token in the current browser tab's session storage. Use HTTPS if exposing the service beyond a trusted local network.

`FILE_SERVER_MAX_UPLOAD_BYTES` is an optional positive integer and defaults to 104857600 (100 MiB) per file. Uploads do not overwrite existing files.

The UI uses HTMX from its pinned public CDN URL. API paths are under `/api`; the OpenAPI schema is available at `/docs`.

## Run with Docker on Raspberry Pi

The Dockerfile uses the official `python:3.12-slim-bookworm` image, which supports both Raspberry Pi 3 Linux architectures: 64-bit (`arm64`) and 32-bit (`arm/v7`). Build and run on the Pi with Docker Compose:

1. Create the host directories and make them writable by the user that will run the container. The defaults match the usual Raspberry Pi OS user; change `PUID` and `PGID` below to the values from `id -u` and `id -g` if needed.

   ```sh
   mkdir -p data/shared data/photos
   sudo chown -R "$(id -u):$(id -g)" data
   cp .env.example .env
   ```

2. Edit `.env` and replace the example token with a long random value. The token is required by the Compose configuration. Keep `.env` private.
3. Build and start the container:

   ```sh
   docker compose up -d --build
   docker compose logs -f
   ```

4. Open `http://<raspberry-pi-address>:8000` on your local network and enter the token. Stop the service with `docker compose down`.

The Compose file mounts `data/shared` and `data/photos` as the `shared` and `photos` roots. Add or change bind mounts and the matching `FILE_SERVER_ROOTS` JSON value together to expose other host directories. The container runs as an unprivileged UID/GID (`1000:1000` by default); set `PUID` and `PGID` in `.env` to match host file ownership when necessary. The app filesystem is read-only; only the mounted data directories are writable.

Compose binds port 8000 to all host interfaces by default for LAN access. You can change `FILE_SERVER_PORT` or set `FILE_SERVER_BIND=127.0.0.1` in `.env`. Use HTTPS via a trusted reverse proxy before exposing the service to an untrusted network. The container allows 512 MiB of temporary upload storage by default; keep this in mind on memory-constrained devices.

To build a multi-architecture image on a machine with Docker Buildx and publish it to a registry:

```sh
docker buildx build --platform linux/arm/v7,linux/arm64 --tag <registry>/<user>/file-server:latest --push .
```

### Raspberry Pi notes

- Use a 64-bit Raspberry Pi OS installation for `arm64`, or a 32-bit installation for `arm/v7`; Docker selects the matching image architecture automatically.
- The single Uvicorn worker keeps memory use modest on the Pi 3.
- HTMX is loaded from a public CDN by the browser, so clients need network access to that CDN.

## API

When a token is configured, send it in the `Authorization: Bearer <token>` header:

- `GET /api/auth/status` — report whether authentication is enabled.
- `GET /api/roots` — list configured roots.
- `GET /api/roots/{root_id}/entries?path=...` — list a directory.
- `GET /api/roots/{root_id}/download?path=...` — download a file.
- `POST /api/roots/{root_id}/directories` — create a directory using `{"path":"relative/parent","name":"new-folder"}`.
- `POST /api/roots/{root_id}/uploads?path=...` — upload one or more multipart `files`.

All paths are relative to the named root. The server rejects traversal outside configured roots and never overwrites an existing upload.

## Tests

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

# Launcher
$env:FILE_SERVER_ROOTS = '{"sandbox":"C:/sandbox/","album":"Z:/2026/"}'
$env:FILE_SERVER_TOKEN = 'replace-with-a-long-random-token'
$env:FILE_SERVER_MAX_UPLOAD_BYTES = '104857600'
python -m uvicorn app:app --host 127.0.0.1 --port 8000