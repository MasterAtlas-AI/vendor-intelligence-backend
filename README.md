# vendor-intelligence-backend

API and background job runner for **MasterAtlas AI**, the enterprise AI vendor intelligence platform.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2 (async, asyncpg) · Alembic · PostgreSQL (Render, with pgvector). No Redis, no Celery, no Docker.

Design documents: [`../docs`](../docs/README.md)

---

## Contents

1. [Status](#status)
2. [Prerequisites](#prerequisites)
3. [Local setup](#local-setup)
4. [Database roles and first-time database setup](#database-roles-and-first-time-database-setup)
5. [Running the app](#running-the-app)
6. [Configuration reference](#configuration-reference)
7. [Tests and quality checks](#tests-and-quality-checks)
8. [Deploying to Render](#deploying-to-render)
9. [Project layout and conventions](#project-layout-and-conventions)
10. [API overview](#api-overview)
11. [Known gaps](#known-gaps)

---

## Status

**Phase 1 (Foundation): done.** **Phase 2 (Vendor management): in progress.** Slices 1–4 of 5 are done: the knowledge base schema, taxonomy and vendor read APIs; proposals and curator approvals; the Excel import; and the PDF import. The vendor UI (in the frontend repo) comes next.

### Phase 1

| Area | What's in place |
|---|---|
| Authentication | Email/password login with Argon2id hashing and a password policy. 15-minute JWT access tokens. Refresh tokens in an httpOnly cookie, rotated on every use; a replayed old token revokes the whole login session (with a short grace window for two tabs refreshing at once). Account lockout, login rate limiting, invitations, password reset and change, logout / logout everywhere. |
| Tenant isolation | The tenant comes only from the verified token or API key. Postgres **Row-Level Security** is enabled and forced on every tenant table. The API connects as a separate role (`app_rw`) that can't bypass it, so even a query that forgets its tenant filter can't see another tenant's rows. |
| Roles & permissions | Tenant roles `tenant_admin`, `manager`, `engineer`, `viewer` (stored in the database, seeded by `bootstrap`). Platform roles `super_admin` and `curator`. Every endpoint checks a permission. Super admins can act inside a tenant with the `X-Act-As-Tenant` header, which is audited. |
| Tenants & users | Platform admins create tenants (which invites the tenant's first admin), suspend and reactivate them. Tenant admins invite, edit, disable users and assign roles. The last tenant admin can't be removed. |
| API keys | Format `mai_live_<prefix>_<secret>`, stored hashed, shown once. Scoped (never broader than the creator), optional expiry and IP allow-list, revocable. |
| Audit log | Append-only (the app role can't update or delete), partitioned by month. Logins, failures, token reuse and every change are recorded in the same transaction as the change. |
| Background jobs | **Postgres is the queue.** The `jobs` table holds every job, and a runner inside the API process claims them with `FOR UPDATE SKIP LOCKED`, so several runners can never take the same job. Each claim is a lease the runner renews while working; if the process dies or is redeployed, the job is picked up again (up to 3 attempts). Also: idempotency keys, cancellation, live progress over Server-Sent Events, and housekeeping (audit partitions, expired tokens) on a timer. A diagnostics job tests it end to end. |
| Operations | Structured logs with request IDs, RFC 7807 error responses, `/health/live` and `/health/ready`, CLI for database role setup, seeding and admin creation. |

### Phase 2 so far

| Area | What's in place |
|---|---|
| Global knowledge base | `vendors`, plus their domains, aliases, locations, industries, equipment, services, certifications, reference projects, sources, and per-fact evidence (`fact_sources`). Shared by all tenants (no `tenant_id`). Facts are retired rather than deleted. |
| Taxonomy | A hierarchical equipment classification (129 nodes, e.g. API 610 pump types OH1-OH6 / BB1-BB5 / VS1-VS7, compressors by API 617/618/619, valves, static, electrical, I&C, packages, offshore systems), 20 service categories, 24 certification types and 14 industries. Codes are dot paths (`pumps.centrifugal.api610.bb2`), so filtering by a parent category includes every descendant. Defined in [app/modules/taxonomy/seed_data.py](app/modules/taxonomy/seed_data.py) and synced by `bootstrap`. |
| Vendor search & detail | Filter by free text (name, alias, website or description), country (HQ or any location), equipment or service category (with descendants), certification, industry and your own approval (AVL) status. Results are sorted alphabetically with cursor pagination. The detail view includes every fact. |
| My vendors (tenant overlay) | Each organization keeps its own approval (AVL) status, internal vendor code, tags and notes for any global vendor, without copying it. Tenant-private under RLS, and audited. |
| Proposals & approvals | The only way data enters the global knowledge base. A proposal is a new or existing vendor plus items: create vendor, add equipment, service, certification, project, location, industry, domain or alias, or change a field. Each item's payload is validated per action, and each can carry evidence (URL, title, snippet, page). Curators (`knowledge:approve`) can edit pending items, approve or reject them per item (rejections need a reason), or ask for more information. Optimistic locking (`version`) stops two curators overwriting each other. Approved items are written to the knowledge base in one transaction. Applying is idempotent per fact, and a website that already belongs to another vendor is refused, so the vendors get merged instead of duplicated. Evidence becomes `vendor_sources` and `fact_sources` rows, and every decision goes to `approval_history` and the audit log. Automated sources (Excel, PDF, AI) call the same service, optionally with automatic approval for trusted tenants. Tenants see their own proposals' status at `/proposals/mine`, never where other proposals came from. |
| Excel import | Upload a vendor list (`.xlsx` or `.csv`, up to 10 MB / 5,000 rows; the file is stored in Postgres). The API detects the header row (even below a title), suggests a column mapping from the header names, and previews the first 20 rows with the predicted outcome of each. The import runs as a background job with live progress, in one transaction, so a crash or cancel leaves nothing half-done. For each row it matches the global knowledge base by website domain, then by exact company name or alias. A match joins the organization's own vendor list with the row's AVL status, vendor code, tags and notes; facts the knowledge base lacks (equipment, services, certifications, industries, aliases, missing country/city/description) are proposed for review. An unknown vendor becomes a new-vendor proposal, and joins the organization's list the moment a curator approves it. Trusted tenants' and curators' imports are approved automatically. Duplicates within the file, names that match several vendors, and vendors already awaiting review are detected. The report lists every row's outcome and reasons (`/imports/{job_id}/rows`, or as CSV). |
| PDF import | Upload a vendor's brochure, datasheet, certificate or prequalification pack (`.pdf`, up to 300 pages read). Rule-based for now (AI extraction arrives in Phase 3): the text layer is matched against the taxonomy's names and synonyms, so it finds equipment types (e.g. API 610 BB2/BB5/VS1), services, certifications (with certificate number and expiry when stated next to them) and industries. Each candidate comes with its page, a text snippet and a confidence. The upload also suggests the company name and website found in the document; scanned PDFs without text are flagged (no OCR yet). You say whose document it is (an existing vendor, or a new one by name and website), untick candidates you don't want, and a background job proposes the rest through the same matching, review and auto-approval rules as the Excel import, with the page and snippet as evidence. |
| Normalisation | Company-name keys that drop legal forms ("Flowserve Corp." = "Flowserve"), registrable web domains from the bundled public-suffix list, and country names to ISO codes. These are the basis for duplicate detection in the Excel import. |

---

## Prerequisites

- **Python 3.12** (`python --version`)
- **A PostgreSQL database** with the `vector`, `pg_trgm`, `citext`, `unaccent`, `btree_gin` and `pgcrypto` extensions available. Render PostgreSQL has all of them.
No Redis, message broker or Docker is required. Background jobs use the same Postgres database.

---

## Local setup

All commands run from this folder (`vendor-intelligence-backend`). The examples use Windows paths (`.venv\Scripts\...`); on macOS/Linux use `.venv/bin/...`.

**1. Create the virtual environment and install dependencies**

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

**2. Create `.env`**

```bash
copy .env.example .env
```

Fill in the values. See [Configuration reference](#configuration-reference). The most important ones:

- `MIGRATION_DATABASE_URL`: Render's **External Database URL**, with `postgresql://` changed to `postgresql+asyncpg://` and `?ssl=require` appended.
- `DATABASE_URL`: the runtime role URL (next section).
- `JWT_SECRET`: generate one with `python -c "import secrets;print(secrets.token_urlsafe(48))"`.

> `.env` is git-ignored. Never commit it or paste it into chats or tickets.

---

## Database roles and first-time database setup

The app uses **two database logins**:

| Login | Used by | Can |
|---|---|---|
| Owner (the user Render created, e.g. `masteratlas_user`) → `MIGRATION_DATABASE_URL` | Alembic migrations and `create-app-role` only | Create tables, extensions and roles |
| Runtime role `app_rw` → `DATABASE_URL` | The API (including its job runner) | Read and write rows only, always subject to Row-Level Security. Can't change the audit log. |

**For a brand-new, empty database**, run these once, in this order:

```bash
# 1. Create the runtime role. Prints the DATABASE_URL to copy into .env.
.venv\Scripts\python -m app.cli create-app-role

# 2. Create the schema, extensions, security policies and grants (uses MIGRATION_DATABASE_URL)
.venv\Scripts\python -m alembic upgrade head

# 3. Seed permissions and system roles, and pre-create audit-log partitions (uses DATABASE_URL)
.venv\Scripts\python -m app.cli bootstrap

# 4. Create your platform Super Admin (prompts for a password)
.venv\Scripts\python -m app.cli create-platform-user --email you@targeticon.com --name "Your Name"
```

**After pulling new code**, run steps 2 and 3 again. Both are safe to repeat.

Running `create-app-role` again **rotates** the runtime role's password. Update `DATABASE_URL` everywhere it's used afterwards.

Password policy: at least 12 characters, at least 3 of lowercase, uppercase, digits and symbols, and it must not contain the part of your email before the @.

---

## Running the app

**API**

```bash
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```

- Interactive API docs: http://localhost:8000/docs
- OpenAPI schema (used by the frontend client generator): http://localhost:8000/api/v1/openapi.json
- Health: http://localhost:8000/health/ready

**Background jobs** need nothing extra: the API starts a job runner on startup (`job_runner: "running"` in `/health/ready`). Queued jobs start within about a second.

To check it, sign in as the super admin and call `POST /api/v1/admin/jobs/diagnostics`. Then open `GET /api/v1/jobs/{job_id}/events` to watch its progress live until `event: completed`.

**Optional: a separate worker process.** If job load ever slows the API down, run jobs in their own process: set `JOB_RUNNER_ENABLED=false` on the API, then start one or more workers (they share the queue safely):

```bash
.venv\Scripts\python -m app.cli run-worker
```

**Make shortcuts** (Git Bash / macOS / Linux): `make install`, `make migrate`, `make bootstrap`, `make dev`, `make worker`, `make test`, `make lint`, `make format`.

---

## Configuration reference

All settings are environment variables (or `.env` entries). They are defined in [app/core/config.py](app/core/config.py).

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `local` | `staging`/`prod` refuse to start with a weak `JWT_SECRET` or insecure cookies |
| `DATABASE_URL` | — | Runtime role (`app_rw`). `postgresql+asyncpg://...?ssl=require` for external connections |
| `MIGRATION_DATABASE_URL` | — | Owner role, used by Alembic and `create-app-role` |
| `APP_DB_ROLE` | `app_rw` | Name of the runtime role that migrations grant privileges to |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `10` | Keep the total under your Render plan's connection limit |
| `JOB_RUNNER_ENABLED` | `true` | Run jobs inside the API. Set `false` only when using `app.cli run-worker` |
| `JOB_CONCURRENCY` | `2` | Jobs run at once per runner |
| `JOB_POLL_SECONDS` | `1.0` | How often an idle runner checks for new jobs (jobs created in the same process start immediately) |
| `JOB_LEASE_SECONDS` | `120` | How long a claim lasts without renewal. After a crash, the job is retried about this long later |
| `MAINTENANCE_INTERVAL_MINUTES` | `360` | Housekeeping interval (audit partitions, expired-token cleanup, linking approved imported vendors) |
| `UPLOAD_MAX_BYTES` | `10485760` | Largest accepted upload (files are stored in Postgres) |
| `IMPORT_MAX_ROWS` | `5000` | Rows read per import; the report says when a sheet had more |
| `PDF_MAX_PAGES` | `300` | Pages of a PDF read for extraction; the rest are skipped (and reported) |
| `JWT_ALGORITHM` | `HS256` | `EdDSA`/`RS256` with `JWT_PRIVATE_KEY` + `JWT_PUBLIC_KEY` |
| `JWT_SECRET` | — | At least 32 characters outside local/dev |
| `JWT_KEY_ID` | `local-1` | Change when rotating signing keys |
| `ACCESS_TOKEN_TTL_SECONDS` | `900` | |
| `REFRESH_TOKEN_TTL_DAYS` | `14` | |
| `REFRESH_COOKIE_SECURE` | `true` | Set `false` only for plain-http localhost |
| `REFRESH_COOKIE_SAMESITE` | `strict` | Use `none` when the frontend and API are on different sites (see Render section) |
| `REFRESH_COOKIE_DOMAIN` | unset | e.g. `.masteratlas.ai` to share the cookie across subdomains |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | JSON list of frontend origins |
| `FRONTEND_URL` | `http://localhost:3000` | Used in invite and reset links |
| `EMAIL_BACKEND` | `console` | `console` logs emails (including links) instead of sending them. `smtp` sends them via `SMTP_*`. |
| `TEST_DATABASE_URL` | unset | Owner URL of a **separate** test database (see below) |

---

## Tests and quality checks

```bash
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check app tests migrations
.venv\Scripts\python -m ruff format --check app tests migrations
.venv\Scripts\python -m mypy app
```

**Unit tests** need nothing.

**Integration tests** (API, tenant isolation, roles and permissions, jobs) need `TEST_DATABASE_URL` in `.env`. It must point at a **dedicated, disposable** database whose name contains `test`. For this project that is `masteratlas_test` on the same Render instance, already configured in `.env`. Every run rolls all migrations down and up again, which **drops every table**. Without `TEST_DATABASE_URL`, integration tests are skipped.

Safety guards: the harness refuses to run if `TEST_DATABASE_URL` points at the same database as `DATABASE_URL` or `MIGRATION_DATABASE_URL`, or if the database name doesn't contain `test`. It uses its own runtime role (`app_rw_test`), so it never touches the real `app_rw` password.

The integration suite covers:
- tenant isolation, including unfiltered queries and cross-tenant writes being blocked by RLS
- the permission matrix for every role
- refresh-token rotation, reuse detection and the two-tab grace window
- lockout, invitation and password-reset flows
- API key scopes and IP allow-lists
- the job queue: claiming, two runners never taking the same job, crash recovery when a lease expires, retry limits, cancellation, idempotency, and the live background loop

Run from outside the US against Render, the full suite takes about 25 minutes, because every query crosses the network. Run single files while working, e.g. `pytest tests/test_jobs.py`.

### CI (GitHub Actions)

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs on every push to `main` and on pull requests. It runs lint, the format check, mypy, a check that migrations have a single head and render to SQL, the OpenAPI export, and the tests.

Integration tests run in CI only if the repository secret **`TEST_DATABASE_URL`** is set (GitHub → Settings → Secrets and variables → Actions). Set it to the owner URL of the disposable test database, e.g. `masteratlas_test`. Without the secret, those tests are skipped and the run still passes. Runs are serialised, because they share that database.

---

## Deploying to Render

One service is enough: it serves the API and runs the background jobs. Create it in the same region as the database (Oregon):

| Service | Type | Build command | Start command |
|---|---|---|---|
| `masteratlas-api` | Web Service (Python) | `pip install .` | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"` |

- **Pre-deploy command** (API service): `alembic upgrade head && python -m app.cli bootstrap`
- **Python version:** pinned by [.python-version](.python-version).
- **Database URLs:** on Render, use the **Internal** database URLs (no `?ssl=require`), e.g. `postgresql+asyncpg://app_rw:...@dpg-xxxx-a/masteratlas`.
- **`--proxy-headers`:** Render terminates TLS in front of the app. Without this flag, every request appears to come from Render's proxy, which breaks audit-log IP addresses, login rate limiting and API-key IP allow-lists.
- **Environment:** set `ENVIRONMENT=prod`, `LOG_JSON=true`, `REFRESH_COOKIE_SECURE=true`, a strong `JWT_SECRET`, `CORS_ORIGINS` and `FRONTEND_URL` for the real frontend, and `EMAIL_BACKEND=smtp` with `SMTP_*` values.
- **Instance type:** use an always-on instance. Render's free web services sleep after about 15 minutes without traffic, which pauses jobs. Interrupted jobs resume when it wakes, but that can take a long time.
- **Scaling later:** add a Render Background Worker with start command `python -m app.cli run-worker` and set `JOB_RUNNER_ENABLED=false` on the API, or run more API instances. Either way, first move rate limiting into Postgres (see Known gaps).
- **Cookies across domains:** if the frontend and API are both on default `*.onrender.com` hosts, they are different sites (`onrender.com` is a public suffix). Then set `REFRESH_COOKIE_SAMESITE=none`. With custom domains under one parent (e.g. `app.masteratlas.ai` and `api.masteratlas.ai`), keep `strict`.

---

## Project layout and conventions

```
app/
  main.py        app factory: middleware, error handlers, routers
  cli.py         create-app-role · bootstrap · create-platform-user · run-worker
  core/          config, logging, errors (problem+json), security (Argon2 / JWT / API keys), pagination, DI container
  api/           dependencies (auth, tenant-bound sessions, require()), request-context middleware, health
  db/            declarative base, mixins (id / created_* / updated_*), tenant-aware session hooks, role provisioning
  domain/        enums, permission catalogue and role bundles, Principal / TenantContext
  infra/         email, rate limiting
  modules/       auth · tenants · users · api_keys · audit · jobs · taxonomy · vendors · tenant_vendors · proposals
  workers/       Postgres job runner (claim / lease / heartbeat), job handlers, maintenance
migrations/      0001 extensions · 0002 tables · 0003 RLS · 0004 audit partitions · 0005 job queue · 0006 knowledge base · 0007 proposals
tests/           unit tests + integration tests against TEST_DATABASE_URL
```

Conventions:

- **Routers are thin.** Services hold the use cases and **commit explicitly**. Audit rows join the same transaction.
- **Sessions:** every session is opened with `open_session(sessionmaker, TenantContext, actor_id)`. Using a session without a tenant context raises an error.
- **Platform sessions:** only `AuthService`, the CLI and the job runner open platform-mode sessions on behalf of callers who aren't super admins.
- **New tenant-owned tables** must have a `tenant_id` column, get an RLS policy in their migration, and get an isolation test.
- **New background work:** add an async handler to `app/workers/handlers.py` for a new `JobType`. Handlers can be re-run after a crash, so make them safe to repeat. Push CPU-heavy work (PDF parsing, large spreadsheets) to a thread with `anyio.to_thread.run_sync` so the API stays responsive.
- **Migrations are hand-written.** Run `alembic upgrade head` and the test suite, which also exercises downgrades, before merging.

---

## API overview

All routes are under `/api/v1`. Full contracts: [`../docs/04-api-contracts.md`](../docs/04-api-contracts.md) and `/docs`.

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/logout-all`, `GET /auth/me`, `POST /auth/password/forgot`, `/auth/password/reset`, `/auth/password/change`, `/auth/invitations/accept` |
| Platform admin | `GET/POST /admin/tenants`, `GET/PATCH /admin/tenants/{id}`, `POST /admin/tenants/{id}/suspend` · `/activate`, `GET /admin/audit-logs`, `POST /admin/jobs/diagnostics` |
| Tenant | `GET/PATCH /tenant` |
| Users & roles | `GET /users`, `POST /users/invite`, `GET/PATCH /users/{id}`, `POST /users/{id}/disable` · `/enable`, `PUT /users/{id}/roles`, `GET /roles`, `GET /permissions` |
| API keys | `GET/POST /api-keys`, `DELETE /api-keys/{id}` |
| Audit | `GET /audit-logs` |
| Jobs | `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `GET /jobs/{id}/events` (SSE) |
| Taxonomy | `GET /taxonomy/equipment-categories`, `/taxonomy/service-categories`, `/taxonomy/certification-types`, `/taxonomy/industries` |
| Vendors | `GET /vendors` (filters: `q`, `country`, `equipment_category`, `service_category`, `certification`, `industry`, `avl_status`), `GET /vendors/{id}` |
| My vendors | `PUT /my-vendors/{vendor_id}` (AVL status, internal code, tags, notes), `DELETE /my-vendors/{vendor_id}` |
| Proposals (curators) | `GET/POST /admin/proposals`, `GET /admin/proposals/{id}`, `PATCH /admin/proposals/{id}/items/{item_id}`, `POST /admin/proposals/{id}/approve` · `/reject` · `/request-info` |
| Proposals (tenants) | `GET /proposals/mine` |

Errors use RFC 7807 (`application/problem+json`) with a machine-readable `code` and the `request_id`.

---

## Known gaps

- **Rate limiting is per process.** That is correct with one API instance. Move it into Postgres before running several instances.
- **Polling:** job progress (SSE) and idle runners each poll Postgres once a second. Postgres `LISTEN/NOTIFY` can replace both later without API changes.
- **Each request re-reads the user, roles and tenant** to authorise. An in-process cache can be added when traffic needs it.
- **Changing your password signs out every session, including the current one.** The client must sign in again.
