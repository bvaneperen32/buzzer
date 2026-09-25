# backend/app/common

## Purpose

Cross-cutting infrastructure that isn't tied to any one feature: auth dependencies, the error
type hierarchy and its handlers, log configuration, and the rate limiter. Small files, wide
blast radius — nearly every router imports from here.

## Contents

- `dependencies.py` — FastAPI auth dependencies. `_token_from_request` pulls a JWT from the
  `Authorization: Bearer` header *or* the `access_token` cookie (header wins). `get_current_user`
  decodes it and re-loads the `User` from the DB. `require_admin` (role must be `ADMIN`) and
  `require_user` (anything but `GUEST`) are the two guards routers depend on.
  `get_refresh_token` reads the `refresh_token` cookie.
- `exceptions.py` — `BuzzerError` base plus `NotFoundError` (404), `ForbiddenError` (403),
  `UnauthorizedError` (401), `ConflictError` (409), and `UnprocessableError` (422, code
  `UNPROCESSABLE` — raised for bad import bundles and invalid merged question edits).
  `register_exception_handlers(app)` wires three handlers: app errors →
  `{"error": code, "message": ...}`, request-validation errors → 422 with
  `{"error": "VALIDATION_ERROR", "detail": [...]}`, and a catch-all → generic 500. **So a 422 comes
  in two shapes**: `UnprocessableError`'s readable `message`, or FastAPI validation's `detail`
  array. The frontends' `api.ts` error helpers read `message`, then `detail`.
- `logging.py` — `configure_logging()` sets up structlog: console renderer in development, JSON
  in production. Called once from [main.py](../main.py).
- `rate_limit.py` — the shared slowapi `limiter`. Routers apply it per-endpoint with
  `@limiter.limit("10/minute")`.

## How it fits in

A horizontal layer under `routers/` and `websocket/`. Note the one upward dependency:
`dependencies.py` imports from `services/auth_service.py`, so `common` is *not* strictly a leaf —
don't import `common.dependencies` from `auth_service` or you'll create a cycle.

Error responses are shaped here, not in routers. Raise a `BuzzerError` subclass and let the
handler format it; don't hand-build `JSONResponse` error bodies in routers.

## Gotchas

- **`get_current_user` accepts `temp` tokens as well as `access` tokens.** Temp tokens are
  issued by the OAuth2 callback and delivered to the browser in a *URL fragment*
  ([routers/auth.py:144](../routers/auth.py#L144)), so they're more exposed than a normal
  access token — yet they authenticate every protected endpoint for their full 15-minute life,
  not just `POST /api/auth/exchange-temp`. Narrowing this to `access` only, with
  `exchange-temp` keeping its own check, would be the tighter design.
- **Rate limiting is effectively off in development.** `_rate_limit_key` returns a fresh UUID per
  request when `APP_ENV=development`, so limits never fire locally — you cannot test rate-limit
  behavior without changing the environment. The same bypass applies in production to any
  request carrying a correct `X-Stress-Key` header, so `STRESS_TEST_KEY` must be unset when not
  actively load-testing.
- **The rate limiter has no shared backend.** slowapi defaults to in-memory storage, so counters
  are per-process and reset on restart; limits are not enforced across multiple workers.
- **Outside development the key is `get_remote_address`, i.e. `request.client.host`.** Behind
  nginx, uvicorn only rewrites that from `X-Forwarded-For` for trusted proxies
  (`--forwarded-allow-ips`, default `127.0.0.1`), and the compose command doesn't set it — so every
  request likely appears to come from the proxy and all clients share one bucket (five logins per
  15 minutes for a whole class). Unverified against the production deployment.
- `configure_logging()` must run before anything logs, or structlog falls back to defaults.
- The validation handler deliberately routes `exc.errors()` through `jsonable_encoder` — Pydantic
  v2 can embed a raw exception object in a field validator's `ctx`, which otherwise turns a
  useful 422 into an opaque 500. The comment in the source explains it; don't "simplify" it away.
- `require_user` allows `ADMIN` through as well as `USER` — it means "not a guest", not "not an
  admin".
- The catch-all `Exception` handler hides real errors behind a generic 500 message. When
  debugging, check the structlog output (`unhandled_error`) rather than the HTTP response.
- `rate_limit.py` imports `settings` lazily *inside* the key function, via the absolute
  `app.config` path rather than a relative import. The code gives no reason, and there is no
  import cycle to avoid (`config.py` imports nothing from `app`). Harmless either way.
