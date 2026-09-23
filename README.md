# ToolLedger

Free, self-hosted QR tool custody for local service shops. Label each tool, scan to check it out to a tech or job, see what’s missing before the vans leave, and export a custody CSV.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Create tools (name, asset tag, category, home location) and import a CSV
- Print a QR label sheet — each QR opens `/t/{token}` using `PUBLIC_BASE_URL`
- Public checkout / return / force-transfer (explicit confirm when already out to someone else)
- People (crew) CRUD for holders
- Missing / overdue list (`OVERDUE_HOURS`, default 24) plus owner **Mark missing**
- Optional BYO SMTP and/or Twilio **Notify missing tools** button
- Append-only events; export custody + events CSV
- `GET /health` → HTTP 200 `{"status":"ok","smtp_configured":false,"twilio_configured":false}` even when SMTP/Twilio unset

## What this is not

- **Not ParKit** — no consumable par sheets or tomorrow’s-job SKU restock
- Not a WMS, RFID fleet, barcode hardware SDK, or Jobber/ServiceTitan inventory sync
- Not tool rental billing (RentBack) or FSM APIs

## Privacy

Self-hosted. You run the box; the owner is the data controller for crew contact fields. No Stripe, no bundled SMS numbers, no third-party analytics SaaS. Data lives in your SQLite file on the Compose volume.

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

```bash
git clone https://github.com/aidendify/toolledger.git
cd toolledger
cp .env.example .env
```

Edit `.env` and set at least `SECRET_KEY`, `OWNER_PASSWORD`, `BUSINESS_NAME`, and `PUBLIC_BASE_URL` (no trailing slash — e.g. `http://YOUR_VPS_IP:8080`). Leave `SMTP_*`, Twilio, and `MARKETING_URL` empty unless configured.

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `toolledger-data` volume at `/data/toolledger.db`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY` and `OWNER_PASSWORD`.

```
OWNER_PASSWORD=testpass
BUSINESS_NAME=Harbor HVAC
PUBLIC_BASE_URL=http://127.0.0.1:8080
OVERDUE_HOURS=24
MARKETING_URL=
SECRET_KEY=change-me
```

Leave all `SMTP_*` and Twilio vars unset.

1. Healthcheck:

   ```bash
   curl -sf http://127.0.0.1:8080/health
   ```

   Expected: JSON containing `"status":"ok"`, `"smtp_configured":false`, `"twilio_configured":false`, HTTP 200.

2. Open http://127.0.0.1:8080, log in with `testpass`. Import `sample-tools.csv` (Tools → Import). Open **Labels** — QR targets use `PUBLIC_BASE_URL`.

3. Add ≥2 people. Open a tool’s public `/t/{token}` URL → check out to person A with an optional job note → dashboard shows **out** to A → return → **available**.

4. Check out to A, then force-transfer to B (explicit confirm). Events record checkout / return / transfer.

5. Mark a tool **Missing** (or lower `OVERDUE_HOURS`) → missing/overdue list non-empty. **Export custody CSV**.

6. Confirm empty `MARKETING_URL` shows no “Powered by” footer on the public tool page. **Notify missing tools** may no-op or say not configured when SMTP/Twilio unset.

## Configuration

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. Container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides to `/data/toolledger.db`. |
| `SECRET_KEY` | Flask session key. Change on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set on internet-reachable VPS. |
| `BUSINESS_NAME` | UI copy. |
| `PUBLIC_BASE_URL` | Absolute base for QR URLs — **no trailing slash**. Required for labels. |
| `OVERDUE_HOURS` | Default 24. Also editable under Settings. |
| `OWNER_EMAIL` + `SMTP_*` | Optional missing notify email. |
| `OWNER_PHONE` + `TWILIO_*` | Optional missing notify SMS. |
| `MARKETING_URL` | Optional footer on public `/t/{token}` only. |

## Local tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest test_app.py -v
```

## License

MIT — free to self-host and modify.
