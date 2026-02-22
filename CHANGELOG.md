# AI Orchestrator - 업데이트 내역 (Phase 7 & Phase 8)

## 1. UI/UX 전면 개편 (다크 모드 대시보드)
- 기존의 요정 컨셉을 벗어나, 전문가(Professional)용 B2B 툴 느낌의 다크 모드(Glassmorphism) UI로 `index.html`을 전면 재작성했습니다.
- 에이전트 파이프라인(PM -> AR -> DEV -> SYS -> QA)이 시각적으로 명확해졌고, 활성화 시 애니메이션을 추가했습니다.
- 실제 IDE를 연상케 하는 모노스페이스 실시간 터미널 로그 뷰를 통해, 코드가 생성되는 과정을 직관적으로 볼 수 있습니다.

## 2. LLM 출력의 100% 구조적 안정성 확보 (Pydantic 도입)
- **개발 요정(Developer Node)**: 텍스트를 슬라이싱하던 기존 방식 대신, `with_structured_output(CodeOutput)`을 사용하여 항상 완벽한 JSON 형태의 파일 트리/소스코드를 반환하도록 강제했습니다.
- **QA 요정(QA Node)**: "STATUS: PASS" 같은 단순 문자열 매칭 대신, `QAReport` 객체를 사용하여 리뷰 텍스트와 통과 여부(boolean)를 분리함으로써 파이프라인의 에러를 원천 차단했습니다.

## 3. 샌드박스 안전성(Security) 강화
- **시스템 요정(Tool Node)**: LLM이 코드를 파일로 저장할 때 `os.path.commonprefix`를 통한 검증 로직을 추가하여, 지정된 `./workspace` 폴더를 벗어나는 상위 디렉토리(Path Traversal) 공격을 완벽하게 방어했습니다.

## 4. 실시간 토큰 및 비용 과금 모니터링
- 모든 노드 호출을 `get_openai_callback()`으로 감싸, 정확히 소모된 API Token 개수와 요금($)을 추적합니다.
- LangGraph의 전체 누적 상태(AgentState)에 비용 데이터를 담아 WebSocket으로 전송하며, 프론트엔드 대시보드 우측 상단 배지(Badge)에 실시간으로 표시됩니다.

# AI Orchestrator - 업데이트 내역 (Phase 9: Next-Level Features)

## 1. Human-in-the-Loop (HITL) 개입 ⏸️
- `langgraph-checkpoint`의 `MemorySaver`를 도입하여, 아키텍트(Architect) 노드 실행 이후 개발 단계 직전에 워크플로우를 일시 정지(`interrupt_before`)합니다.
- 프론트엔드에서 모달 창을 띄워 유저가 직접 소프트웨어 구조를 검토 및 수정하고 `/api/resume`으로 상태를 재개하도록 만들었습니다.

## 2. 통합 다중 파일 코드 에디터 (Monaco) 연동 📝
- CDN을 통해 Microsoft Monaco Editor(VS Code 엔진)를 탑재했습니다.
- 로그 화면과 나란히 배치되는 Split-Pane UI로 파일 트리를 구성하여, AI가 생성한 여러 개의 소스 코드를 클릭하여 실시간으로 문법 렌더링된 코드를 확인할 수 있습니다.

## 3. 격리된 안전한 샌드박스 실행 (Docker Python SDK) 🐳
- Tool Node의 기존 로컬 `subprocess` 실행 코드를 `docker` Python SDK를 이용한 `python:3.11-slim` 도커 컨테이너 실행으로 전면 교체했습니다.
- 마운트 드라이브 읽기 전용, 네트워크 격리를 적용하여 완전 무결하고 안전한 환경에서 파이썬 문법 테스트가 이뤄집니다.

## 4. GitHub 자동 리포지토리 배포 🐙
- `PyGithub`를 통해 자동 배포 노드를 새롭게 구성하여, QA 심사 통과 시 작동합니다.
- 발급받은 `GITHUB_ACCESS_TOKEN`을 기반으로 새 Repo를 찍어내고 `workspace` 안의 생성된 파일을 자동으로 Push 함으로써 CI/CD의 마지막 퍼즐을 맞췄습니다.
