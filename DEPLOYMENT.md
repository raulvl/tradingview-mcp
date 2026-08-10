# Self-Hosting tradingview-mcp on Railway

A complete guide to deploying the TradingView MCP server on Railway's cloud platform using GitHub integration.

## Prerequisites

- A [GitHub](https://github.com) account
- A [Railway](https://railway.com) account (free tier gives $5 credit ~500 hours/month)
- An optional [Marketaux](https://www.marketaux.com) API key (for news/sentiment features)

## Architecture

```
GitHub Repo (your fork)
    │
    │  Push to main → auto-deploy
    ▼
Railway Builder (Dockerfile)
    │
    │  Multi-stage Python 3.11-slim build
    ▼
Railway Container
    │
    │  streamable-http on $PORT
    ▼
┌─────────────────────────────────────────┐
│  tradingview-mcp-yourname.up.railway.app │
│                                          │
│  GET /health    → health check (200)     │
│  POST /mcp      → MCP protocol endpoint  │
│  GET /sse       → SSE streaming          │
└─────────────────────────────────────────┘
    │
    ▼
Claude Desktop / Cursor / ChatGPT / any MCP client
```

## Step 1: Fork the Repository

1. Go to https://github.com/atilaahmettaner/tradingview-mcp
2. Click **Fork** (top-right corner)
3. Select your GitHub account as the destination
4. Leave **Copy the `main` branch only** checked
5. Click **Create fork**

You now have your own copy at `https://github.com/<your-username>/tradingview-mcp`.

## Step 2: Clone Your Fork & Apply Railway Config

Run the following commands in your terminal (Windows PowerShell or Git Bash):

```bash
# Clone your fork
git clone https://github.com/<your-username>/tradingview-mcp.git
cd tradingview-mcp

# Copy in the Railway deployment files
# (These files are provided in this repo's root)

# Verify the following files exist:
#   - Dockerfile          (modified for dynamic PORT)
#   - railway.json        (Railway service configuration)

# Commit and push
git add Dockerfile railway.json
git commit -m "Add Railway deployment configuration"
git push origin main
```

### Dockerfile Changes Explained

The original Dockerfile hardcoded port 8000 in both the `HEALTHCHECK` and `CMD` instructions. Railway injects a `PORT` environment variable at runtime with a dynamically assigned port. Our changes:

**CMD** — changed from exec-form to shell-form so `$PORT` is expanded at container start:

```dockerfile
# Before:
CMD ["streamable-http", "--host", "0.0.0.0", "--port", "8000"]

# After:
CMD ["sh", "-c", "exec tradingview-mcp streamable-http --host 0.0.0.0 --port ${PORT:-8000}"]
```

**HEALTHCHECK** — uses the same `$PORT` variable to hit the correct port:

```dockerfile
# Before:
CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# After:
CMD sh -c 'python -c "import urllib.request; urllib.request.urlopen(\"http://localhost:${PORT:-8000}/health\")"' || exit 1
```

The `${PORT:-8000}` syntax means "use `$PORT` if set, otherwise fall back to `8000`" — this keeps local `docker build` / `docker-compose` working.

### railway.json Explained

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": {
    "builder": "DOCKERFILE",
    "dockerfilePath": "Dockerfile"
  },
  "deploy": {
    "numReplicas": 1,
    "healthcheckPath": "/health",
    "restartPolicyType": "ON_FAILURE"
  }
}
```

| Field | Purpose |
|-------|---------|
| `builder` | Tells Railway to use the Dockerfile (not Nixpacks) |
| `numReplicas` | 1 instance (scale up for higher availability) |
| `healthcheckPath` | Railway pings `/health` every 30s to detect failures |
| `restartPolicyType` | `ON_FAILURE` — restarts only on crash, not on idle timeout |

## Step 3: Install & Authenticate Railway CLI

```bash
# Install Railway CLI globally via npm
npm install -g @railway/cli

# Login — this opens a browser window
railway login

# Verify you're logged in
railway whoami
```

## Step 4: Create a Railway Project & Link Your Repo

### Option A: Via Railway Dashboard (Recommended)

1. Go to https://railway.com and log in
2. Click **New Project**
3. Choose **Deploy from GitHub repo**
4. Click **Configure GitHub App** (one-time setup)
   - Grant access to your fork of `tradingview-mcp`
5. Select your `tradingview-mcp` repository
6. Railway reads your `Dockerfile` and `railway.json` automatically
7. Click **Deploy** — the first build takes ~3-5 minutes

### Option B: Via Railway CLI

```bash
# Create a new project
railway init

# Or link an existing folder to a new Railway project
railway link

# Deploy
railway up
```

## Step 5: Configure Environment Variables

In the Railway dashboard, go to your project → **Variables** tab and add:

| Variable | Value | Required |
|----------|-------|----------|
| `MARKETAUX_API_TOKEN` | Your Marketaux API key | Only for news/sentiment |
| `MARKETAUX_DAILY_BUDGET` | `90` | Optional |
| `PROXY_ENABLED` | `false` | Optional (disable proxy for Railway) |

**Do NOT add `PORT`** — Railway sets this automatically.

> **Getting a Marketaux API key:** Sign up at https://www.marketaux.com — the free tier gives 100 requests/day. Without it, `financial_news` and `market_sentiment` tools return a "not configured" message; all 35 other tools work normally.

## Step 6: Verify the Deployment

Once the build completes, Railway assigns a public URL like:

```
https://tradingview-mcp-production.up.railway.app
```

Test the health endpoint:

```bash
curl https://your-app.up.railway.app/health
# Expected: {"status": "ok"}  or  HTTP 200
```

Check logs in the Railway dashboard under your service → **Deployments** → click the latest deployment → **Build & Deploy Logs**.

## Step 7: Configure Your MCP Client

### Claude Desktop

Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tradingview": {
      "url": "https://your-app.up.railway.app/mcp"
    }
  }
}
```

**Location by OS:**
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Then restart Claude Desktop.

### Cursor

In Cursor settings → **MCP** → **Add new MCP server**:

```json
{
  "mcpServers": {
    "tradingview": {
      "url": "https://your-app.up.railway.app/mcp"
    }
  }
}
```

### ChatGPT / Copilot / Any SSE-compatible client

Use the SSE endpoint:

```
https://your-app.up.railway.app/sse
```

## Step 8: Test It

Ask Claude (or your MCP client) something like:

```
Show today's top crypto gainers on Binance
Run a full technical analysis of NVDA
What's the market snapshot right now?
```

## Resource Usage & Costs

| Plan | RAM | CPU | Price | Good for |
|------|-----|-----|-------|----------|
| Free trial | 512 MB | Shared | $5 credit | Testing, personal use |
| Hobby | 512 MB | Shared | $5/mo | Personal daily use |
| Pro | 1-2 GB | vCPU | $20/mo+ | Multiple concurrent users |

The Docker image is ~200-300MB. At idle, the server uses ~100-150MB RAM.

## Troubleshooting

### Build fails with `curl` or `uv` errors
Wait and retry. Railway's builder network may be temporarily slow fetching from `astral.sh`.

### Container starts but healthcheck fails
This usually means the port binding failed. Check logs:
```
railway logs
```
Look for `OSError: [Errno 98] Address already in use` or similar.

### "Server not found" in MCP client
Ensure you're using the `/mcp` path in the URL:
```
https://your-app.up.railway.app/mcp    ← correct
https://your-app.up.railway.app         ← wrong (no MCP endpoint)
```

### Railway container sleeps / cold starts
On the free/hobby tier, Railway may scale to zero after inactivity. The first request "wakes" the container (~5-10s cold start). To prevent this, upgrade to a paid plan with minimum 1 replica, or set up a cron job (e.g., UptimeRobot) to ping `/health` every 5 minutes.

## Updating Your Deployment

Simply push to your fork's `main` branch:

```bash
git add .
git commit -m "Update tradingview-mcp"
git push origin main
```

Railway auto-detects the push and redeploys within seconds.

## Important Notes

- **No TradingView account needed** — this server fetches public market data, it does not log into or scrape TradingView
- **Not financial advice** — all outputs are informational/educational
- **Rate limits** — upstream TradingView APIs may throttle heavy concurrent use; the server has built-in throttling (4 concurrent TA calls, 0.8s spacing)
