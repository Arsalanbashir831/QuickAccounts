# API authentication

The API uses 10-minute `Bearer` access JWTs and per-device, rotating opaque refresh
sessions in the configured Redis cache. Django admin remains on Django's separate
session/CSRF authentication. Ordinary API calls validate the signed JWT and user
revision, without a Redis read. Sensitive views can opt into
`apps.identity.permissions.ActiveRefreshSession` for immediate session revocation.

The browser first requests `GET /api/auth/csrf/` and keeps the returned CSRF token
in memory. `POST /api/auth/login/` accepts `email` and `password`, returns `access`
in JSON, and sets an HttpOnly refresh cookie. Keep the access token only in app
memory and send it as `Authorization: Bearer ...`. `POST /api/auth/refresh/` reads
the cookie, requires `X-CSRFToken`, rotates the credential, and returns a new
access token. `POST /api/auth/logout/` also requires `X-CSRFToken`; it revokes
the matching refresh session and clears the cookie. The refresh secret is never
returned in JSON or stored in Redis un-hashed. Reuse of a rotated secret revokes
that device's token family. Concurrent duplicate refreshes therefore yield one
successful rotation and one reuse rejection; the successful replacement is also
revoked. A frontend should serialize refresh requests per browser session.

`POST /api/auth/logout-all/`, `GET /api/auth/sessions/`,
`DELETE /api/auth/sessions/{session_id}/`, and
`POST /api/auth/password/change/` require a bearer access token. Password
change revokes all refresh sessions and increments `auth_revision`, immediately
invalidating previously issued access JWTs. Other logout actions revoke refresh
sessions immediately; previously issued access JWTs remain usable until their
10-minute expiry on ordinary endpoints. The versioned `/api/v1/auth/` paths are
aliases; the old `token` URLs accept the new cookie-based contract, not a refresh
JWT request body.

Environment settings:

VPS settings load the project-root `.env` automatically for local commands when present;
variables supplied by the process or deployment environment take precedence.
For example, run `uv run python manage.py check` and then
`uv run python manage.py runserver 127.0.0.1:8000 --insecure` from the project
root. The `--insecure` flag serves Django admin CSS/JS during local development
when `DJANGO_DEBUG=false`; do not use it for production serving. For deployment,
run `uv run python manage.py collectstatic --noinput` and serve `staticfiles/`
at `/static/` from the web server.
To apply new migrations, use the owner connection rather than the runtime role:
`set -a; source .env; set +a; DATABASE_URL="$DATABASE_MIGRATION_URL" uv run python manage.py migrate`.

- `JWT_SIGNING_KEY`: independent high-entropy signing key, required in production.
- `AUTH_REFRESH_SESSION_SECONDS`: sliding refresh lifetime (default 30 days).
- `AUTH_REFRESH_COOKIE_NAME`, `AUTH_REFRESH_COOKIE_PATH`,
  `AUTH_REFRESH_COOKIE_SECURE`, `AUTH_REFRESH_COOKIE_SAMESITE`: cookie policy.
- `AUTH_LOGIN_FAILURE_LIMIT`, `AUTH_LOGIN_FAILURE_WINDOW_SECONDS`: Redis login
  failure threshold and window.
- `DJANGO_CSRF_TRUSTED_ORIGINS`: comma-separated trusted origins if a separate
  HTTPS frontend is deployed.

The default is same-origin browser deployment, `Secure` cookie, `SameSite=Lax`,
cookie path `/api/`. A cross-site frontend also requires an explicit credentialed
CORS configuration, an HTTPS origin, and `SameSite=None`; no origin is assumed or
wildcarded here. Redis must remain private. A Redis outage fails login, refresh,
and session operations closed with HTTP 503. Password resets or administrative
password changes added elsewhere must call the shared `revoke_all_sessions`
helper and increment `auth_revision`; the included password-change endpoint does
both. A future password-reset endpoint should never reveal whether an email
exists.

For a plain-HTTP local development browser, set
`AUTH_REFRESH_COOKIE_SECURE=false` only in that local environment. Never deploy
that override to HTTPS production.
