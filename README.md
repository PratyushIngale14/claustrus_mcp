# Claustrus

**Universal grounding and citation verification for AI agent outputs.**

A small FastAPI service that checks every sentence of an AI-generated
rationale against provided source documents, cites what is grounded,
flags what is not, and logs the full audit trail in an EU AI Act
Article 12 compatible format.

> **Note on "MCP" in the name:** despite the repo name, this is a plain
> REST API, not a server that speaks the actual Model Context Protocol
> (JSON-RPC `initialize` / `tools/list` / `tools/call`). The
> `/.well-known/mcp.json` route is just a descriptive manifest, not a
> real MCP transport. If you want this reachable from Claude Code /
> Claude Desktop's native `mcpServers` config, you'd need to wrap these
> endpoints with the official `mcp` Python SDK (stdio or SSE transport).
> As shipped, integrate it the way you'd integrate any HTTP API — see
> "Using it from your own tool" below.

---

## Endpoints

| Endpoint | Method | What it does |
|---|---|---|
| `/ground` | POST | Core grounding — any domain, any source documents |
| `/ground/healthcare` | POST | Grounding + 8 CMS 2026 compliance rules |
| `/logs/download?format=ndjson` | GET | Download full audit log as NDJSON |
| `/logs/download?format=json` | GET | Download full audit log as JSON array |
| `/logs/summary` | GET | Summary stats of all logged calls |
| `/health` | GET | Health check |
| `/.well-known/mcp.json` | GET | Descriptive manifest listing the two tools above |

---

## Run locally

```bash
git clone https://github.com/PratyushIngale14/claustrus_mcp.git
cd claustrus_mcp
pip install -r requirements.txt
uvicorn server:app --reload
```

Server starts at http://localhost:8000

Interactive API docs (Swagger UI) at http://localhost:8000/docs

### Environment variables (all optional)

| Variable | Purpose | Default |
|---|---|---|
| `CLAUSTRUS_API_KEY` | If set, every request must send a matching `x-api-key` header, or it gets a 401. If unset, the API is open. | none |
| `CLAUSTRUS_LOG_PATH` | Where the NDJSON audit log is written. | `/tmp/claustrus_audit.ndjson` |

There is no server-side Anthropic API key. The optional semantic pass
(see "Token efficiency" below) is powered by an `anthropic_api_key`
field passed in the *request body* per call — the server never reads
an Anthropic key from its own environment.

### Run with Docker

```bash
docker build -t claustrus .
docker run -p 8000:8000 -e CLAUSTRUS_API_KEY=your-key claustrus
```

---

## Using it from your own tool

This is a plain HTTP API, so any tool, script, or agent that can make
an HTTP request can use it — curl, Python `requests`, a Claude Code
Bash tool call, a LangChain/OpenAI custom tool definition, a Zapier
webhook, etc. There's no client library or special SDK required.

Minimal shape:

```bash
curl -X POST http://localhost:8000/ground \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-key" \
  -d '{"rationale": "...", "sources": {"DOC-ID": "..."}}'
```

Only send the `x-api-key` header if you set `CLAUSTRUS_API_KEY` on the
server; otherwise omit it.

---

## Example — core grounding call

```bash
curl -X POST http://localhost:8000/ground \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-key" \
  -d '{
    "rationale": "The MRI is covered under advanced imaging services. The headache diagnosis supports medical necessity.",
    "sources": {
      "SEC-5.1": "Advanced imaging including MRI requires prior authorization before the date of service."
    },
    "claim_id": "CLM-1001"
  }'
```

Response includes:
- `faithfulness_score` — 0.0 to 1.0
- `status` — PASSED / WARNING / BLOCKED
- `citations` — per-sentence grounding with source excerpt and reason
- `eu_ai_act_fields` — Article 12 compliant logging fields
- `human_summary` — clean readable summary
- `latency_ms` — end-to-end processing time

---

## Example — healthcare compliance call

```bash
curl -X POST http://localhost:8000/ground/healthcare \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-key" \
  -d '{
    "rationale": "The MRI is approved. It is covered under advanced imaging services.",
    "sources": {"SEC-5.1": "Advanced imaging including MRI requires prior authorization."},
    "claim_id": "CLM-1001",
    "decision": "approve",
    "billed_amount": 2400,
    "prior_authorization": null,
    "policy_clause_ids": ["SEC-5.1"],
    "diagnosis_code": "R51.9",
    "procedure_code": "70553",
    "state": "TX"
  }'
```

`decision` and `billed_amount` are required fields for this endpoint;
everything else is optional. Returns the grounding result plus eight
compliance rule results (R1-R8), each with the exact CMS regulatory
source that the rule is derived from.

---

## Download audit logs

```bash
# NDJSON — one line per call, best for log tools (grep, jq, Splunk)
curl http://localhost:8000/logs/download?format=ndjson \
  -H "x-api-key: your-key" -o audit.ndjson

# JSON array — best for manual review or spreadsheet import
curl http://localhost:8000/logs/download?format=json \
  -H "x-api-key: your-key" -o audit.json
```

Each log entry includes the EU AI Act Article 12 required fields:
input data hash, system ID, faithfulness score, status, and whether
human oversight is required.

---

## EU AI Act Article 12 compliance

Every call produces an `eu_ai_act_fields` block:

```json
{
  "article": "Article 12 — Record-keeping",
  "input_data_hash": "sha256 of the rationale text",
  "system_id": "claustrus-core-v1.0.0",
  "faithfulness_score": 0.67,
  "status": "BLOCKED",
  "human_oversight_required": true,
  "timestamp_utc": "2026-07-24T05:00:00+00:00"
}
```

This is logged to the NDJSON file at `CLAUSTRUS_LOG_PATH` and
downloadable on demand via `/logs/download`. The combination of input
hash, system ID, score, status, and timestamp provides the evidence
trail Article 12 requires for high-risk AI systems.

---

## Deploy to Railway (or any container host)

1. Push this repo to GitHub
2. Go to railway.app, create new project from GitHub repo
3. Optionally set `CLAUSTRUS_API_KEY` to lock down the API
4. Deploy — Railway detects the `Dockerfile` automatically
5. Your API is live at the Railway-provided URL

There's no other required environment variable — the Anthropic key,
if you want the optional semantic pass, is supplied per-request by
whatever client calls the API, not configured server-side.

---

## Token efficiency

The deterministic token-overlap grounding pass costs zero API tokens.
Claude is only called when:
- `anthropic_api_key` is provided in the request, AND
- At least one sentence is flagged as unsupported

In that case, Claude is called once per flagged sentence using
`claude-haiku-4-5` with an 80-token max response.

Typical call costs:
- No API key: 0 tokens, under 1ms latency
- With API key, all sentences grounded: 0 tokens
- With API key, one flagged sentence: ~150 tokens, 300-500ms

---

## License

MIT. Use it, build on it, contribute back.

## Author

Pratyush Ingale
github.com/PratyushIngale14
