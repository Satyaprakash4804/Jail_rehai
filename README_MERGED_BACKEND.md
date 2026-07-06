# Jail Rehai Backend — Web + Flutter (merged, drop-in)

This replaces 3 files in your backend folder. Everything else
(`accused_common.py`, `admin.py`, `super_admin.py`, `master.py`, `auth.py`,
`db.py`, `utils.py`, `config.py`, `fcm_service.py`, `firebase_config.py`,
templates, static) is untouched — your web app keeps working exactly as
it does today.

## Files in this zip

| File | Action |
|---|---|
| `run.py` | **Replace** your existing `run.py` with this one. |
| `bail_bulk.py` | **Replace** your existing `bail_bulk.py` with this one. |
| `mobile_api.py` | **New file** — add it next to `run.py`. |

## What changed in `run.py`

1. Added session-cookie config so a logged-in session survives app
   restarts and works over HTTPS for a non-browser client (the Flutter
   app uses `dio` + a persisted cookie jar — same session cookie your
   web app already uses, no new auth system):
   ```python
   app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
   app.config['SESSION_COOKIE_SAMESITE'] = 'None'
   app.config['SESSION_COOKIE_SECURE'] = True
   ```
   `SAMESITE='None'` requires HTTPS (`SECURE=True`). If you're testing
   locally over plain `http://`, temporarily set `SECURE=False` and
   `SAMESITE='Lax'` — flip them back for production.
2. Registered the new blueprint:
   ```python
   from mobile_api import mobile_bp
   app.register_blueprint(mobile_bp, url_prefix='/api/mobile')
   ```
   Every other route, blueprint, and the request-logging middleware is
   byte-for-byte the same as your current file.

## What changed in `bail_bulk.py`

One bug fix: `confirm_batch()` referenced `r['row_number']`, but the
`bail_excel_row` table column is actually `row_no`. This was a
pre-existing `KeyError` that would crash bail-excel batch confirmation
on the **web app too**, not just the app — it just hadn't been hit yet.
Fixed by using the correct column name. No other logic changed.

## What `mobile_api.py` adds

A JSON API surface at `/api/mobile/...` for the Flutter app, reusing your
existing `db.py`, `utils.py`, `accused_common.py`, and `bail_bulk.py`
functions wherever they're already data-only (notifications, bail-excel
batch review/confirm/discard/pending-photos), and mirroring the same SQL
queries as `admin.py`/`super_admin.py`/`master.py` wherever those files
mix querying with `render_template` (accused, FIR, dashboards, admin
management, activity logs) — so the app and the web app always see
identical data and identical authorization rules (same
`@role_required` logic, same session, same district-scoping).

Full endpoint list:

**Auth** — `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`,
`POST /auth/forgot-password`, `POST /auth/verify-otp`,
`POST /auth/change-password`

**Dashboard** — `GET /dashboard` (auto-detects master / super_admin /
admin from the session and returns the right stats)

**Notifications** — `GET /notifications`, `POST /notifications/mark-read`,
`GET /notifications/count`
(FCM device-token endpoints are unchanged — `/api/fcm/save-token` and
`/api/fcm/delete-token` already existed in `run.py` and are used as-is.)

**Accused** — `GET /accused`, `GET /accused/<id>`,
`POST /accused/<id>/upload-photo`, `POST /accused/<id>/approve-bail`,
`POST /accused/<id>/revoke-bail`, `GET /bailed-accused`

**FIR** — `GET /fir`, `GET /fir/<id>`, `POST /fir/add`

**Excel bulk** — `POST /upload-accused`, `GET /download-accused-sample`,
`POST /bail-excel/upload`, `GET /bail-excel/batches`,
`GET /bail-excel/batch/<id>`,
`POST /bail-excel/batch/<id>/row/<row_id>/resolve`,
`POST /bail-excel/batch/<id>/confirm`,
`POST /bail-excel/batch/<id>/discard`, `GET /bail-pending-photos`,
`POST /bail-pending-photos/<id>/complete`

**Master** — `GET /master/super-admins`,
`POST /master/create-super-admin`, `GET|POST /master/edit-user/<id>`,
`POST /master/revoke-user/<id>`, `GET /master/all-admins`,
`GET /master/logs`

**Super admin** — `GET /super/admins`, `POST /super/create-admin`,
`POST /super/admin/<id>/toggle`, `POST /super/upload-admins`,
`GET /super/download-admin-sample`

## Deploy steps

1. Drop in the 3 files above.
2. `pip install -r requirements.txt` (no new dependencies were added —
   everything used already ships with your existing `requirements.txt`).
3. Restart Flask (or your WSGI server / systemd service / reload ngrok
   tunnel target). No DB migration needed — no schema changes.
4. Confirm the web app still works exactly as before (login, dashboards,
   bail approval, etc.) — since nothing in the existing blueprints was
   touched, this should need no verification beyond a smoke test.
5. Point the Flutter app's `ApiConfig.baseUrl` at this server and it will
   authenticate and pull data through `/api/mobile/...` immediately.
