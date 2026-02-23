# AI Orchestrator

## 모드

현재 UI/운영 포커스는 `Network Chat PoC`입니다.

- 분산 워커는 Redis Streams를 통해 통신합니다.
- 대시보드에서 모드 전환 없이 네트워크 실행을 바로 시작합니다.
- 호환성을 위해 기존 Graph 워크플로우 코드는 백엔드에 유지되어 있습니다.

## 분산 Network Chat PoC

### 서비스

`docker-compose.yml`에는 다음 서비스가 포함됩니다.

- `redis`
- `agent_pm`
- `agent_architect`
- `agent_developer_backend`
- `agent_developer_frontend`
- `agent_developer`
- `agent_tool_execution`
- `agent_qa`
- `agent_github_deploy`

### 런타임 설정

워커 관련 환경 변수:

- `REDIS_URL` (기본값: `redis://redis:6379/0`)
- `NETWORK_TASK_GROUP` (기본값: `network-workers`)
- `NETWORK_TASK_CONSUMER_NAME` (선택값, 미지정 시 자동 생성)
- `NETWORK_TASK_MAX_ATTEMPTS` (기본값: `3`)
- `NETWORK_TASK_READ_BLOCK_MS` (기본값: `2000`)
- `NETWORK_TASK_READ_COUNT` (기본값: `10`)
- `NETWORK_TASK_CLAIM_MIN_IDLE_MS` (기본값: `30000`)
- `NETWORK_TASK_CLAIM_INTERVAL_MS` (기본값: `10000`)
- `NETWORK_QA_MAX_RETRIES` (기본값: `3`)
- `NETWORK_NODE_TIMEOUT_SEC` (기본값: `420`)
- `NETWORK_ENABLE_PARALLEL_DEVELOPERS` (기본값: `true`)
- `NETWORK_APPROVAL_GATES` (기본값: 빈 값, 예: `pm,architect,qa`; 지정 단계 완료 후 디렉터 승인 전 대기)
- `NETWORK_MOCK_MODE` (기본값: `false`, CI/로컬 결정적 dry-run 모드)
- `NETWORK_MOCK_QA_FAIL` (기본값: `false`, true일 때 mock QA가 1회 실패 후 통과)
- `LLM_MAX_ATTEMPTS` (기본값: `2`)
- `LLM_RETRY_BACKOFF_SEC` (기본값: `1.0`)
- `OPENAI_FALLBACK_MODELS` (기본값: `gpt-4.1,gpt-4o-mini`)
- `PROMPT_MAX_DOC_CHARS` (기본값: `12000`)
- `PROMPT_MAX_LOG_CHARS` (기본값: `6000`)
- `PROMPT_MAX_SOURCE_FILES` (기본값: `20`)
- `PROMPT_MAX_CHARS_PER_FILE` (기본값: `3000`)
- `PROMPT_MAX_TOTAL_SOURCE_CHARS` (기본값: `24000`)

선택적 LangSmith 트레이싱:

- `LANGCHAIN_TRACING_V2=true`
- `LANGCHAIN_API_KEY=<your_key>`
- `LANGCHAIN_PROJECT=ai-orchestrator`
- `LANGCHAIN_ENDPOINT=https://api.smith.langchain.com` (선택)

### API

- `POST /api/network/start`
  - 본문: `{ "prompt": "...", "selected_model": "gpt-4o-mini" }`
- `GET /api/network/runs/{run_id}`
- `POST /api/network/runs/{run_id}/approval`
  - 본문: `{ "action": "approve|reject", "actor": "director-ui", "note": "...", "reject_to_role": "architect" }`
- `GET /api/network/runs/{run_id}/events`
- `WS /ws/network/{run_id}`
- `GET /api/network/admin/queues`
  - 선택 쿼리: `?role=pm`
- `GET /api/network/admin/dlq/{role}`
  - 쿼리: `?limit=100`
- `POST /api/network/admin/dlq/{role}/requeue`
  - 본문: `{ "entry_id": "...", "target_role": "pm", "reset_attempt": true, "delete_from_dlq": true }`

### UI

대시보드는 현재 `Network Chat PoC`에만 집중합니다.
프롬프트를 입력하고 모델을 선택한 뒤 네트워크 실행을 시작할 수 있습니다.
큐 메트릭과 DLQ 재처리를 위한 Operations 패널도 제공합니다.
`NETWORK_APPROVAL_GATES`가 설정된 경우, 지정 단계 뒤에 승인 대기 패널(승인/반려 버튼)이 표시됩니다.

기본 실행 경로는 병렬 팀 협업을 지원합니다.
`pm -> architect -> (developer_backend || developer_frontend) -> merge -> tool_execution -> qa -> github_deploy`

### 회귀 평가

분산 API를 대상으로 멀티 프롬프트 평가를 실행합니다.

```bash
python3 backend/scripts/evaluate_network_runs.py \
  --api-base http://127.0.0.1 \
  --model gpt-4o-mini \
  --output backend/scripts/eval_report.json
```

`--prompts-file`로 JSON/JSONL/TXT 프롬프트 파일을 전달할 수도 있습니다.

### CI Mock 검증

OpenAI 쿼터 없이 병렬 워크플로우를 검증하려면 다음을 실행하세요.

```bash
python3 backend/scripts/check_parallel_mock_flow.py
```

이 스크립트는 내부적으로 `NETWORK_MOCK_MODE=true`를 활성화하고 아래를 검증합니다.
- 병렬 fan-out (`developer_backend`, `developer_frontend`)
- fan-in merge 완료
- 최종 상태가 `completed`인지 여부
