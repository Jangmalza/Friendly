# AI Orchestrator 전체 시스템 검토 및 잠재적 문제점 분석

현재 구축된 AI Orchestrator(Phase 9)는 훌륭한 프로토타입이며 상용 서비스의 기반을 갖췄습니다. 하지만 실제 프로덕션(Production) 환경에 배포하거나 다수의 사용자를 수용할 때 발생할 수 있는 주요 **아키텍처, 성능, 보안, UX 측면의 한계 및 문제점**들을 다음과 같이 종합 요약합니다.

## 1. LLM 출력의 물리적 한계 (Token Limit Truncation)
- **현재 구조**: `developer_node`에서 모든 소스 코드 파일을 하나의 JSON 객체(`Dict[str, str]`)로 한 번에 응답받습니다.
- **문제점**: 프로젝트가 조금만 커져도(파일 3~4개 이상) LLM의 개별 응답 최대 토큰 한도(예: 4,096~16,384 토큰)를 초과하게 됩니다. 이 경우 JSON이 중간에 잘려(Truncate) Pydantic 파싱 에러가 발생하고 코드를 잃게 됩니다.
- **해결책**:
  - `Plan & Execute` 패턴 적용: 설계자가 먼저 파일 목록(Plan)만 생성하고, 이후 Loop를 돌며 각 파일별로(Execute) 코드를 개별 생성하여 토큰 한도를 우회해야 합니다.

## 2. 상태 영속성 (State Persistence) 결여
- **현재 구조**: LangGraph의 상태 저장을 위해 인메모리 파이프라인(`MemorySaver`)을 사용 중입니다.
- **문제점**: FastAPI 서버가 재시작되거나 에러로 크래시가 나면, 현재 진행 중이거나 Human-in-the-Loop로 대기 중이던 모든 워크플로우 상태(Thread ID)가 즉시 소멸합니다.
- **해결책**:
  - `PostgresSaver`나 `SqliteSaver`, `Redis` 등 영구적인 DB 기반 Checkpointer로 교체해야 합니다.

## 3. 동기(Synchronous) 블로킹 코드로 인한 FastAPI 병목 현상
- **현재 구조**: `tool_execution_node`의 Docker SDK(`client.containers.run`)와 `github_deploy_node`의 PyGithub API가 **동기(Sync) 방식**으로 작성되어 있습니다.
- **문제점**: FastAPI는 비동기 처리(Event Loop)가 강점인데, 도커 컨테이너를 띄우거나 깃허브 API를 기다리는 동안 쓰레드가 블로킹되어 다른 유저의 웹소켓 요청이 지연되거나 타임아웃될 수 있습니다.
- **해결책**:
  - `asyncio.to_thread()`를 사용해 백그라운드 쓰레드에서 블로킹 작업을 실행하거나, 비동기 라이브러리(ex: `aiohttp`, `aiodocker`)를 사용해야 합니다.

## 4. Docker 샌드박스의 불완전한 환경
- **현재 구조**: `python3 -m py_compile`을 통해서 파이썬 **문법(Syntax) 검사만** 수행하고 있습니다.
- **문제점**:
  - 실제 코드를 실행(`/app/main.py`)하여 런타임 오류나 논리적 무한 루프를 탐지하지 못합니다.
  - 외부 라이브러리(`requests`, `fastapi` 등)를 임포트하는 코드를 생성했을 경우, 컨테이너 환경 내에 해당 패키지가 설치(`pip install`)되어 있지 않아 실행 시 무조건 `ModuleNotFoundError`가 뜹니다.
- **해결책**:
  - AI가 `requirements.txt`도 함께 생성하도록 유도한 뒤, 컨테이너에서 임시 venv를 만들고 의존성을 설치하여 실제 실행 테스트(ex: `pytest`)를 진행하는 형태로 고도화가 필요합니다.

## 5. 보안(Security) 및 권한 리스크
- **현재 구조**: FastAPI 서버가 호스트에서 `docker` 데몬 권한을 갖고 있어야 `docker.from_env()`가 작동합니다.
- **문제점**: 백엔드 애플리케이션 자체가 해킹될 경우 호스트 머신의 도커 데몬 및 전체 루트(Root) 권한이 탈취될 수 있는 위험(Privilege Escalation)이 존재합니다.
- **해결책**:
  - 백엔드를 직접 도커 데몬에 물리는 대신, 서버와 독립된 격리된 Firecracker MicroVM 단위를 사용하거나 권한이 철저히 제한된 별도의 Job Queue 워커 노드에서 코드를 실행하도록 분리해야 합니다.

## 6. GitHub 배포 자동화의 오남용 (Spamming)
- **현재 구조**: QA 통과 시마다 매번 새로운 이름(`ai-generated-app-2026...`)으로 Public Repository를 생성합니다.
- **문제점**: 유저가 수정 요청을 여러 번 반복하거나 시스템을 남용할 경우, 엄청난 수의 쓰레기 Repo가 생성되어 GitHub API Rate Limit에 걸리거나 보안 제재를 받을 수 있습니다.
- **해결책**:
  - 매번 새 Repo를 파는 대신, 하나의 사용자별 기준 Repo를 두고 PR(Pull Request) 단위로 커밋을 쌓는 등 버저닝(Versioning) 전략이 필요합니다.

## 7. 클라이언트(웹소켓) 연결 안정성
- **현재 구조**: `index.html`과 FastAPI가 단순 WebSocket 연결을 맺고, LLM 응답을 무한정 대기합니다.
- **문제점**: 긴 코드를 생성하는 동안 네트워크에 트래픽이 없으면 브라우저나 Ngnix 프록시가 Idle Timeout으로 강제로 웹소켓 연결을 끊어버릴 수 있습니다 (Ping/Pong 메커니즘 부재).
