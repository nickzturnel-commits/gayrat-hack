# Secure X VPN subscription relay

FastAPI service with the original raw subscription relay and an independent admin panel. The relay keeps its existing behavior at `/`, `/sub.txt`, and `/health`; its request profile and HWID header forwarding are separate from panel synchronization.

The admin panel manages subscriber profiles and servers in Supabase. Changes made in the panel are pushed immediately to Google Drive through a protected Apps Script Web App. The existing five-minute Apps Script trigger remains as a recovery path. The already-connected legacy Drive file is kept as **Основная (legacy)** so its existing URL remains unchanged. Its real file ID is stored by the application and is intentionally omitted from this documentation. Example only: `YOUR_EXISTING_DRIVE_FILE_ID`.

## Render environment

Create or update the Render Web Service from `render.yaml`. Set these environment values in the service dashboard; secret values are never stored in this repository:

| Variable | Required | Purpose |
| --- | --- | --- |
| `SUBSCRIPTION_URL` | Yes for relay and admin sync | Provider subscription URL |
| `UPSTREAM_USER_AGENT` | No | Provider request User-Agent; panel uses `Happ` when empty |
| `UPSTREAM_X_HWID` | No | Fixed authorized device identity |
| `UPSTREAM_X_DEVICE_INFO` | No | Optional upstream device metadata |
| `UPSTREAM_X_DEVICE_MODEL` | No | Optional upstream device model |
| `UPSTREAM_X_DEVICE_OS` | No | Optional upstream operating system |
| `UPSTREAM_X_VER_OS` | No | Optional upstream OS version |
| `UPSTREAM_X_APP_VERSION` | No | Optional client version |
| `DATABASE_URL` | Yes for panel data | Supabase PostgreSQL URL |
| `ADMIN_USERNAME` | Yes for panel | Login name |
| `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` | Yes for panel | Login password or supported password hash |
| `SESSION_SECRET` | Yes for panel | Long random value used to sign sessions |
| `SYNC_KEY` | Yes for Apps Script | Shared secret sent as `X-Sync-Key` |
| `DRIVE_SYNC_URL` | Yes for instant panel updates | Deployed Apps Script Web App URL ending in `/exec` |
| `DRIVE_API_KEY` | Yes to show Drive subscription links | Google Drive API key, returned only to authenticated administrators |

`ADMIN_PASSWORD_HASH`, when set, takes precedence over `ADMIN_PASSWORD`. Supported encodings are `pbkdf2_sha256$salt$urlsafe_base64_digest` (310,000 PBKDF2-SHA256 rounds) and `scrypt$salt$urlsafe_base64_digest` (`n=16384, r=8, p=1`). Keep the hash string in Render secrets just like a password. Use a random `SESSION_SECRET` of at least 32 characters. If the admin credentials or `SESSION_SECRET` are missing or invalid, panel API access is disabled. If `SYNC_KEY` is missing, Apps Script endpoints are disabled. The relay itself remains available if the database cannot connect.

Use a Supabase connection string from **Project → Connect**. Render may not be able to reach a direct `db.<project>.supabase.co` host over IPv6. In that case choose **Session pooler** in Supabase and put that connection string in `DATABASE_URL`; the service accepts both direct connections and pooler users such as `postgres.<project-ref>`. The connection is made with SSL and individual connection parameters rather than passing the URL to the driver. For a password with spaces or reserved URL characters, percent-encode them (` ` → `%20`, `@` → `%40`, `%` → `%25`). Raw spaces and `@` in the password are parsed too, but percent-encoding is preferred. Never paste the real database password into GitHub, documentation, issues, or logs.

On first successful database connection the service runs `schema.sql` idempotently, seeds settings, and creates the legacy subscriber if it is not already present. The pool is small and lazy; temporary database failures do not stop the relay. The admin page reports a safe connection error and the Session Pooler recommendation.

## Local setup

1. Install Python 3.10+ and run `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and fill the values needed for your environment. Do not commit `.env`.
3. Run `uvicorn app:app --host 0.0.0.0 --port 8000`.
4. Open `https://<service>/admin` (or the local URL) and log in with `ADMIN_USERNAME` and the configured password.

## Google Apps Script installation

The source is [`apps_script/Code.gs`](apps_script/Code.gs). Replace the contents of the existing Apps Script project with this file; keep the existing `updateSub` time trigger set to every five minutes as a recovery path.

1. In Apps Script open **Project Settings → Script Properties** and add `RENDER_URL` with value `https://gayrat-hack.onrender.com` and `SYNC_KEY` with the same secret as the Render environment variable. Do not put either secret in `Code.gs`.
2. Save the script, choose `setup` in the function selector, and run it once. It validates the properties and deliberately does not create or delete triggers.
3. Run `updateSub()` once and approve the Google Drive and external request permissions. This initial run updates existing files and creates files for current users.
4. In Apps Script click **Deploy → New deployment → Web app**. Set **Execute as** to your account and **Who has access** to **Anyone**, then deploy and approve access. Copy the Web App URL ending in `/exec`.
5. In Render add `DRIVE_SYNC_URL` with that `/exec` URL and redeploy. `SYNC_KEY` must match the Script Property. The Web App checks this secret on every request; do not share the URL together with the key.
6. Keep the existing `updateSub` trigger. Thereafter user, server, and profile setting changes made in `/admin` call the Web App immediately; no manual Apps Script run is needed. The trigger retries as a backup.

When updating an already deployed script, use **Deploy → Manage deployments → Edit → New version → Deploy** so the existing `/exec` deployment URL remains the same. Revoking a profile updates the Drive file's media with Drive API v3 using the Apps Script user's OAuth token. Drive API v3 must be enabled in the Cloud project linked to this Apps Script project, and the script owner must authorize Drive access again if Google asks. The same Drive file ID and public URL are kept so Happ can fetch the revocation profile on its next refresh.

The Web App receives targeted updates for an individual subscriber and updates all profiles when global server/settings values change. It fetches the manifest from Render, skips file writes when the contents are identical, and reports Drive IDs and results back to Render. New users get a text file shared as **Anyone with the link — Viewer**; existing files are updated in place. A user you manually delete in the panel is marked `pending_delete`; Apps Script replaces the content of that same Drive file with one nonworking server named **Подписка отключена**, reports success, and Render then removes the user row. The subscription URL therefore remains valid and refreshes to the disabled profile instead of returning 404, which could leave Happ using its last imported servers. If the provider is unavailable, Render still returns pending deletions to Apps Script so they can be revoked while active profiles remain untouched. The five-minute `updateSub()` trigger remains a repair/recovery path.

Apps Script compares the current file text with generated content and skips `setContent` when it is identical. Apps Script fetches Ashgabat's current temperature from Open-Meteo and passes the small, validated weather snapshot to Render with the authenticated manifest request. This avoids relying on Render's shared outbound IP, which Open-Meteo says may hit free API limits earlier ([Open-Meteo terms](https://open-meteo.com/en/terms)). The request asks for `temperature_2m` and includes the source's valid time; if a WMO weather code is present, Render maps it to a Russian condition description, but does not guess a condition when the API omits it. Apps Script caches weather for four minutes to avoid duplicate requests during a sync burst; its normal trigger runs every five minutes. Provider HTTP status and a bounded error reason are written to the Apps Script execution log without logging the complete response. Quotes, weekday/greeting, and footer rotate on stable `announce_refresh_minutes` time slots (default 5 minutes), so a delayed trigger does not keep the previous slot's quote. If weather is unavailable, the announcement explicitly says so instead of presenting an old value as current. Drive is written only when the generated text actually changes. For 10 or more users, 60 minutes is recommended to reduce Drive writes and Apps Script runtime quota use. The Apps Script execution keeps a user cursor if it approaches the six-minute runtime limit and resumes on the next trigger.

Happ reads the latest saved Drive file; refreshing it does not itself call Render or Apps Script. The existing five-minute Apps Script trigger is what fetches new weather and updates that file, so the client sees the newest generated conditions after the next trigger and Drive update. Open-Meteo provides model-based current conditions and WMO codes, not local-station observations; the announcement includes the data's valid time and rejects responses older than two hours or more than 15 minutes in the future.

## Panel and subscription behavior

- **Dashboard:** upstream cache and warning, provider account traffic, subscriber counts, instant Drive delivery status, database status and delivery errors.
- **Users:** add/edit, enabled state, expiry, profile title and announcement overrides, generated preview, Drive link, QR code for each ready Drive profile, and deletion. The Drive URL is preloaded in the authenticated admin list so the mobile copy action runs directly in the tap gesture; if the browser blocks it, the panel opens a selectable link field. User changes are sent to Drive immediately.
- **Servers:** global rename/hide/order and per-user rename/hide. Only servers in the latest successful upstream snapshot are shown or offered for per-user overrides. If the provider changes a server URI but keeps its name, the existing server identity and global/per-user customizations are retained.
- **Settings:** profile title, support URL, update interval, timezone, announcements, refresh interval, weekday lines, quotes and footer templates.
- `/api/sync/manifest` fetches the panel's upstream subscription itself whenever the separate 5-minute panel cache is empty or stale. It uses only configured `UPSTREAM_*` device headers and the fallback User-Agent `Happ`. The request timeout is 20 seconds. A good previous copy is served on refresh failure. HTML, device-limit responses and bodies without supported servers are rejected and never replace a good cache. The panel accepts plain or Base64 URI lists and Xray JSON profiles containing Shadowsocks outbounds; for JSON it creates standard `ss://` entries from `method`, `password`, `address` and `port`, using `remarks` as the server name. Raw spaces in provider URI fragments are accepted and safely URL-encoded in generated Happ profiles. If there is no good copy yet, manifest returns an error so Apps Script can retry without overwriting Drive.
- Relay cache and panel upstream cache are separate. The original relay continues to pass the upstream response body and selected headers through as before.

Generated Google Drive profiles show the provider's `subscription-userinfo` usage by default. The **Основная (legacy)** profile always shows it. For a user profile, the admin can turn on **Скрывать счётчик трафика в Happ**; the setting **Скрывать счётчик по умолчанию** applies only to newly created profiles and can be overridden per user. The shown upload, download, quota and provider expiry are the provider account's shared values, not per-user measurements. A custom expiry set in the panel is also shown in the generated profile. After expiry, the profile remains stored and contains one nonworking placeholder server named **Время подписки закончилось**. Expired profiles are never automatically removed. Deletion in the panel replaces the file contents with one nonworking **Подписка отключена** server and removes the user row after Apps Script confirms the update. Clients must refresh the subscription to receive that replacement; this service cannot remotely erase credentials already imported by a client, and the VPN links connect directly to the provider's servers. Per-user traffic measurement/enforcement and device usage are not available through the shared Drive URL.

The admin UI has separate desktop and mobile pages. `/admin` selects a layout using viewport width and pointer capability; `/admin/desktop` and `/admin/mobile` open either layout directly. Shared CSS and JavaScript are served as local static assets without a build step or third-party CDN.

## Migration without changing the existing link

1. Configure Render environment variables and deploy this version. Keep the current Apps Script running while Render builds; the public relay endpoints are unchanged.
2. Confirm `/health` returns `ok`, then open `/admin` and confirm the database status is connected. If it is unavailable, use the Supabase Session Pooler URL.
3. Confirm the dashboard reports a successful upstream snapshot and a nonzero server count. A failed/empty manifest is not sent to Apps Script as a valid user list, so it cannot overwrite the current Drive file.
4. Replace the old Apps Script source, set the two Script Properties, run `setup()` and `updateSub()` once, then deploy it as a Web App and set its `/exec` URL in Render as `DRIVE_SYNC_URL`. The legacy subscriber continues writing to its existing Drive file; its Drive API URL does not change.
5. Confirm the old Happ link still loads, then create additional users in the panel. Their Drive files and links are created immediately by the Web App.

The project has no dependency on an Apps Script trigger recreation: the existing five-minute trigger name remains `updateSub`.

## Tests

Run the isolated test suite with `python -m unittest -v`. Network calls in the new panel tests use local HTTP mocks. They do not access the real provider subscription, Google, or Supabase. No database integration test runs unless one is explicitly added and configured separately.
