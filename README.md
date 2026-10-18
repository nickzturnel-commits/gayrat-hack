# VPN subscription relay

The service fetches a VPN subscription from `SUBSCRIPTION_URL`, caches the upstream response, and serves it from `/` or `/sub.txt`. It preserves the subscription body and relevant upstream headers so VPN clients can consume the original format.

## Local use

1. Install Python 3.10 or newer and run `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and set `SUBSCRIPTION_URL` to the private upstream subscription URL.
3. Run `uvicorn app:app --host 0.0.0.0 --port 8000`.
4. Add `http://localhost:8000/` or `http://localhost:8000/sub.txt` to the VPN client.

On the first GET request, the relay fetches the upstream subscription and forwards supported client headers, including User-Agent and device headers. It caches the body and response headers in memory, then refreshes the cache every 15 minutes using the same headers. If a refresh fails, the last successful response remains available. Before the first successful fetch, subscription requests return an error. `HEAD` requests and `/health` do not fetch the upstream.

## Render deployment

Create a Render Web Service from this repository using `render.yaml`. Set `SUBSCRIPTION_URL` in the service environment; keep it private. Use the Render service URL as the subscription URL in the VPN client.

The cache is in memory. A sleeping or restarted free service fetches a fresh snapshot on the next GET request, then resumes its 15-minute refresh cycle. The application does not log the subscription URL or response body.
