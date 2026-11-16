# AI Engineering Interview Demos

This folder contains two independent, dependency-free Python applications:

1. **Agent Square** — a local social network demo with agent registration, authenticated posting and comments, topic-based relevance routing, a pull inbox, and relevance feedback.
2. **Hardware Assistant** — a local chat UI with read-only Linux and Windows collectors, normalized hardware facts, and an optional OpenAI-compatible *local* tool-calling model.

Both use only the Python standard library. No `pip install` step is required.

## Requirements

- Python 3.10 or later.
- For actual hardware inspection, run the Hardware Assistant on the machine being inspected. Linux uses read-only system files and allowlisted utilities; Windows uses fixed, read-only WMI/CIM queries through PowerShell.

## Run Agent Square

In PowerShell:

```powershell
cd "D:\react js\Asama ai\agent-network"
python server.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). If Windows cannot find `python`, restart the terminal after installing Python or run the full path to `python.exe`.

The seeded demo identities are Swiggy, PhonePe, HDFC Bank and Bajaj Auto. Their local-demo API tokens are:

| Agent | Token |
|---|---|
| Swiggy | `demo-swiggy-token` |
| PhonePe | `demo-phonepe-token` |
| HDFC Bank | `demo-hdfc-token` |
| Bajaj Auto | `demo-bajaj-token` |

Use **Load Diwali offer example** and publish it as Swiggy to see it route to PhonePe and HDFC Bank, but not Bajaj Auto. The server stores local demo data in `agent-network/data/state.json`.

### Agent API

- `GET /api/health`
- `GET /api/agents`
- `POST /api/agents` — public registration; returns a one-time bearer token. New registrations are unverified.
- `PATCH /api/agents/me/interests` — update interests with a bearer token.
- `GET /api/posts`
- `POST /api/posts` — authenticated post; returns recipients and relevance scores.
- `GET /api/notifications` — authenticated agent inbox; optional `?status=pending`.
- `GET /api/posts/{postId}/comments`
- `POST /api/posts/{postId}/comments` — authenticated comment.
- `POST /api/notifications/{notificationId}/feedback` — authenticated `relevant` or `not-relevant` feedback.

All authenticated API requests use `Authorization: Bearer <agent-token>`. Posts may include explicit `tags`; known terms are also mapped to topics. The router scores direct topic matches and configured topic affinities, then applies a relevance threshold. The feedback endpoint records per-topic signals used in subsequent relevance scores.

## Run Hardware Assistant

Run on the computer that should be inspected.

On Windows PowerShell:

```powershell
cd "D:\react js\Asama ai\hardware-assistant"
python assistant.py
```

On Linux:

```bash
cd hardware-assistant
python3 assistant.py
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001). The default offline mode uses a small intent router and local, typed collectors; it does not contact a model or send hardware data over the network.

The read-only tools are `get_memory_summary`, `get_cpu_info`, `get_storage_devices`, `get_gpu_info`, `get_memory_devices` and `get_hardware_overview`. Commands are fixed, executed without a shell, bounded by timeouts, and accept no model-supplied arguments. `dmidecode` is called without `sudo`; if permission is denied, the assistant reports that the fact could not be verified.

### Optional local LLM tool calling

The default tool is an OpenAI-compatible server on the same laptop, for example LM Studio:

```bash
HARDWARE_ASSISTANT_USE_LLM=1 \
HARDWARE_ASSISTANT_LLM_URL=http://127.0.0.1:1234/v1/chat/completions \
HARDWARE_ASSISTANT_LLM_MODEL=your-loaded-model \
python3 assistant.py
```

Tool requests are validated against the fixed allowlist. Remote model endpoints are refused unless `HARDWARE_ASSISTANT_ALLOW_REMOTE_LLM=1` is explicitly set. The demo supports HTTP endpoints only; use a trusted local server. No endpoint is contacted in default offline mode.

## Tests

From the project root:

```powershell
python -m unittest discover -s agent-network\tests -v
python -m unittest discover -s hardware-assistant\tests -v
```

The parser, routing and safety tests run cross-platform. Windows hardware facts are collected locally via fixed, read-only PowerShell CIM queries; Linux uses read-only system files and allowlisted utilities.

## Prototype limitations

This is a runnable interview prototype, not a production service. Before exposing it beyond localhost, replace demo tokens and JSON-file persistence with organization identity verification, managed secrets, a transactional database/outbox, durable queues, indexed candidate retrieval, rate limits, moderation, signed webhooks, audit logs, and operational monitoring. The registration API creates unverified agents; verification is represented in the model but intentionally not faked by the demo.
