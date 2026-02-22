# AI Orchestrator

## Mode

Current UI/operation focus is `Network Chat PoC`:

- Distributed workers communicate through Redis streams.
- Dashboard starts network runs directly (no mode toggle).
- Legacy Graph workflow code still exists in backend for compatibility.

## Distributed Network Chat PoC

### Services

`docker-compose.yml` now includes:

- `redis`
- `agent_pm`
- `agent_architect`
- `agent_developer`
- `agent_tool_execution`
- `agent_qa`
- `agent_github_deploy`

### Runtime Configuration

Worker-related environment variables:

- `REDIS_URL` (default: `redis://redis:6379/0`)
- `NETWORK_TASK_GROUP` (default: `network-workers`)
- `NETWORK_TASK_CONSUMER_NAME` (optional; auto-generated if omitted)
- `NETWORK_TASK_MAX_ATTEMPTS` (default: `3`)
- `NETWORK_TASK_READ_BLOCK_MS` (default: `2000`)
- `NETWORK_TASK_READ_COUNT` (default: `10`)
- `NETWORK_TASK_CLAIM_MIN_IDLE_MS` (default: `30000`)
- `NETWORK_TASK_CLAIM_INTERVAL_MS` (default: `10000`)
- `NETWORK_QA_MAX_RETRIES` (default: `3`)
- `NETWORK_NODE_TIMEOUT_SEC` (default: `420`)
- `LLM_MAX_ATTEMPTS` (default: `2`)
- `LLM_RETRY_BACKOFF_SEC` (default: `1.0`)
- `OPENAI_FALLBACK_MODELS` (default: `gpt-4.1,gpt-4o-mini`)
- `PROMPT_MAX_DOC_CHARS` (default: `12000`)
- `PROMPT_MAX_LOG_CHARS` (default: `6000`)
- `PROMPT_MAX_SOURCE_FILES` (default: `20`)
- `PROMPT_MAX_CHARS_PER_FILE` (default: `3000`)
- `PROMPT_MAX_TOTAL_SOURCE_CHARS` (default: `24000`)

Optional LangSmith tracing:

- `LANGCHAIN_TRACING_V2=true`
- `LANGCHAIN_API_KEY=<your_key>`
- `LANGCHAIN_PROJECT=ai-orchestrator`
- `LANGCHAIN_ENDPOINT=https://api.smith.langchain.com` (optional)

### API

- `POST /api/network/start`
  - body: `{ "prompt": "...", "selected_model": "gpt-4o-mini" }`
- `GET /api/network/runs/{run_id}`
- `GET /api/network/runs/{run_id}/events`
- `WS /ws/network/{run_id}`
- `GET /api/network/admin/queues`
  - optional query: `?role=pm`
- `GET /api/network/admin/dlq/{role}`
  - query: `?limit=100`
- `POST /api/network/admin/dlq/{role}/requeue`
  - body: `{ "entry_id": "...", "target_role": "pm", "reset_attempt": true, "delete_from_dlq": true }`

### UI

Dashboard is now focused on `Network Chat PoC` only.
Enter a prompt, choose model, and start a network run.
The dashboard also provides an Operations panel for queue metrics and DLQ redrive.

### Regression Evaluation

Run multi-prompt evaluation against the distributed API:

```bash
python3 backend/scripts/evaluate_network_runs.py \
  --api-base http://127.0.0.1 \
  --model gpt-4o-mini \
  --output backend/scripts/eval_report.json
```

You can also pass `--prompts-file` with JSON/JSONL/TXT prompts.
