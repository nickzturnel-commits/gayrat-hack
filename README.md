# VPN subscription relay

The service fetches a VPN subscription from `SUBSCRIPTION_URL`, caches the upstream response, and serves it from `/` or `/sub.txt`. It preserves the subscription body and relevant upstream headers so VPN clients can consume the original format.

## Local use

1. Install Python 3.10 or newer and run `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and set `SUBSCRIPTION_URL` to the private upstream subscription URL.
3. Run `uvicorn app:app --host 0.0.0.0 --port 8000`.
4. Add `http://localhost:8000/` or `http://localhost:8000/sub.txt` to the VPN client.

On a GET request, the relay fetches the upstream subscription with supported client headers, including User-Agent and device headers such as `X-HWID`. The shared in-memory cache is refreshed if a request arrives with a different forwarded header profile, so an earlier browser request cannot leave a web page cached for a VPN client. HTML responses are rejected instead of being cached as subscription data. Other upstream bodies, including a device-limit message, are passed through unchanged. The relay refreshes the cache every 15 minutes using the most recently successful client profile; if a refresh fails, the last successful response remains available. Before the first successful fetch, subscription requests return an error. `HEAD` requests and `/health` do not fetch the upstream.

Opening the relay URL in a regular browser sends the browser's User-Agent, which may cause the upstream service to return its web interface; the relay reports this as an error and does not cache that page. Add the relay URL to the VPN client so it can send its own User-Agent and device headers. If the upstream reports that its device limit is full, the relay forwards that response unchanged; it cannot turn the provider's device-limit message into usable server links. Render logs identify whether the request looked like Happ or a browser and whether an HWID was present, without printing header values, the subscription URL, or its response body. The official Happ documentation describes HWID forwarding and User-Agent configuration: https://github.com/HappDev/happ_su/blob/main/dev-docs/app-management.md.

To make a browser or other caller use a fixed client profile, optionally set `UPSTREAM_USER_AGENT` and `UPSTREAM_X_HWID` in Render's environment settings. Device fields `UPSTREAM_X_DEVICE_INFO`, `UPSTREAM_X_DEVICE_MODEL`, `UPSTREAM_X_DEVICE_OS`, `UPSTREAM_X_VER_OS`, and `UPSTREAM_X_APP_VERSION` are also supported. Each configured value overrides the matching incoming header. Use values belonging to your own authorized Happ device; do not invent an HWID. If these variables are empty, headers from the incoming VPN-client request are forwarded as before. Configured values are never written to logs.

## Render deployment

Create a Render Web Service from this repository using `render.yaml`. Set `SUBSCRIPTION_URL` in the service environment; keep it private. Use the Render service URL as the subscription URL in the VPN client.

The cache is in memory. A sleeping or restarted free service fetches a fresh snapshot on the next GET request, then resumes its 15-minute refresh cycle. The application does not log the subscription URL or response body.
