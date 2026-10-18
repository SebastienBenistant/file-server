# File Server Specification

## 1. Purpose

Build a small, self-hosted file server that lets an authorized user browse and manage files under one or more explicitly configured base directories. The service provides a browser-based UI and an HTTP API, both hosted by the Python application.

## 2. Goals

- Browse configured base directories and their descendants.
- Download files the user is permitted to access.
- Create directories and upload files into configured directories.
- Optionally require one shared bearer token for access.
- Keep deployment and the web UI lightweight, with no separate frontend build or service.

## 3. Proposed technology

- **Backend:** Python and FastAPI.
- **UI:** HTML templates served by FastAPI, using Jinja2 and HTMX for lightweight interactions. A small amount of browser JavaScript may be used for bearer-token handling and file uploads.
- **Configuration:** Environment variables, including a mapping of user-visible root names to filesystem paths.
- **Packaging:** A Python dependency manifest and a documented command to run the application.

This is the proposed minimal stack; the UI framework can be changed before implementation if desired.

## 4. Users and access model

- The service has one shared access policy; per-user accounts, roles, and permissions are out of scope.
- When a bearer token is configured, API and file-operation requests must supply it as `Authorization: Bearer <token>`.
- When no token is configured, the service is unauthenticated. This mode is intended only for trusted networks or local use.
- The UI must let the user provide the configured token and use it for protected requests. The token must not be included in URLs.
- Invalid or missing credentials receive HTTP `401` for API requests.

## 5. Functional requirements

### 5.1 Configured roots

- An operator configures one or more base directories, each with a stable display name.
- Only configured roots and their descendants are visible and addressable.
- The UI presents the configured roots as the top level of navigation.
- A missing or inaccessible configured root is reported clearly; it must not silently become a different directory.

### 5.2 Browse

- The user can list a directory's immediate child directories and files.
- Each entry displays its name and whether it is a file or directory. File size and modification time may also be shown when available.
- The user can navigate into child directories and back to parent directories, including switching between configured roots.
- Empty directories are shown as empty; filesystem and permission errors are presented clearly.

### 5.3 Download

- The user can download any regular file visible under a configured root.
- Downloads preserve the original filename.
- Directories and unsupported filesystem objects cannot be downloaded as files.
- A request for a path outside configured roots is rejected.

### 5.4 Create directories

- The user can create a directory in the currently browsed directory.
- Directory names must be a single valid path component, not an absolute path or a path containing traversal components.
- Creating a directory that already exists returns a clear conflict error and does not modify existing content.

### 5.5 Upload files

- The user can upload one or more files into the currently browsed directory.
- The destination must be within a configured root and writable by the server process.
- Uploading a file whose name already exists must not overwrite the existing file; the request reports a conflict.
- Upload failures are reported to the user. A partially received upload must not be presented as a successful completed file.
- The server enforces a configurable maximum upload size, defaulting to 100 MiB per file.

## 6. Security and filesystem constraints

- Resolve and validate every requested path against its configured root on the server; never rely on client-side path validation.
- Reject path traversal and paths outside the configured roots for listing, downloading, directory creation, and upload.
- Do not follow symlinks that resolve outside the configured root. Symlink behavior for targets that remain inside a root should be consistent across operations.
- Do not expose absolute server filesystem paths in the UI or normal API responses.
- Treat uploaded filenames as untrusted input and prevent them from selecting a different destination.
- The service does not provide arbitrary file editing, deletion, renaming, archive extraction, or shell access.
- Production deployment should use HTTPS when accessed over an untrusted network, since bearer tokens are credentials.

## 7. API outline

The exact route names and response schemas will be finalized during implementation. The API should provide equivalent capabilities to:

- `GET /api/roots` — list configured roots.
- `GET /api/roots/{root_id}/entries?path=...` — list a directory.
- `GET /api/roots/{root_id}/download?path=...` — download a file.
- `POST /api/roots/{root_id}/directories` — create a child directory.
- `POST /api/roots/{root_id}/uploads?path=...` — upload one or more files into a directory.

All mutating endpoints and file-content endpoints follow the configured bearer-token policy. API errors use appropriate HTTP status codes and a consistent, non-sensitive error response.

## 8. User interface

- A single-page layout shows configured roots, current location, directory entries, and actions to create a directory or upload files.
- The user can navigate by selecting directory entries and use a breadcrumb or equivalent control to return to ancestors.
- The UI displays progress or a clear result for uploads, and clear error messages for failed actions.
- If authentication is enabled, the UI provides a token-entry flow and does not place the token in the URL.
- The UI is usable on common desktop and mobile browser sizes; accessibility and visual polish should remain appropriate for a small utility.

## 9. Operational requirements

- The application can be run as one Python process serving both UI and API.
- Host, port, configured roots, and optional token are supplied through documented configuration.
- The application logs operational errors without logging bearer tokens or file contents.
- The service does not require a database.

## 10. Acceptance criteria

1. With two configured roots, the UI and API show both roots and can browse their directories independently.
2. A user can download a file under a root and receives the original filename.
3. A user can create a child directory and upload a new file into a browsed directory.
4. Attempts to traverse outside a root are rejected for every operation, including traversal through symlinks.
5. Existing files are not overwritten by uploads, and existing directories are not replaced.
6. When a token is configured, requests without it or with an incorrect token are rejected; requests with the correct token succeed.
7. When no token is configured, requests work without credentials.
8. Missing roots, permission failures, invalid input, and upload errors are reported without exposing server absolute paths.

## 11. Out of scope for the first version

- User accounts, per-user authorization, and audit history.
- File deletion, renaming, editing, or sharing links.
- Search, previews, thumbnails, and archive extraction.
- Remote storage backends, database-backed metadata, and multi-process coordination.
- Public-internet deployment hardening beyond the documented token and HTTPS requirements.

## 12. Decisions to confirm

The following decisions are accepted for the first version:

- Use FastAPI with Jinja2 templates and HTMX; do not introduce a separate frontend build.
- Configure named roots through environment variables.
- Limit each uploaded file to 100 MiB by default; make the limit configurable.
- Reject symlinks that resolve outside a configured root and allow symlinks that resolve inside it.
- Keep the bearer token in browser session storage, so it survives page reloads in the current tab but is cleared when that tab's session ends.
