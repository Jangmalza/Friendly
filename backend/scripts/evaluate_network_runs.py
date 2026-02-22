#!/usr/bin/env python3
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_PROMPTS = [
    "두 수를 더하는 파이썬 함수와 테스트를 만들어줘",
    "간단한 TODO REST API 서버와 테스트 코드를 작성해줘",
    "CSV를 읽어 요약 통계를 출력하는 파이썬 스크립트와 테스트를 생성해줘",
]


@dataclass
class CaseResult:
    prompt: str
    run_id: str
    requested_model: str
    final_model: Optional[str]
    status: str
    duration_sec: float
    files_generated: int
    fallback_used: bool
    error: Optional[str]


def _http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(url=url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Unexpected response type for {url}")
    return parsed


def _load_prompts(prompts_file: Optional[str]) -> List[str]:
    if not prompts_file:
        return list(DEFAULT_PROMPTS)

    path = Path(prompts_file)
    if not path.exists():
        raise FileNotFoundError(f"prompts file not found: {prompts_file}")

    prompts: List[str] = []
    text = path.read_text(encoding="utf-8")

    if path.suffix.lower() == ".json":
        parsed = json.loads(text)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    prompts.append(item.strip())
                elif isinstance(item, dict) and isinstance(item.get("prompt"), str):
                    prompts.append(item["prompt"].strip())
        elif isinstance(parsed, dict):
            for key in ("prompts", "cases"):
                value = parsed.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.strip():
                            prompts.append(item.strip())
                        elif isinstance(item, dict) and isinstance(item.get("prompt"), str):
                            prompts.append(item["prompt"].strip())
        else:
            raise ValueError("Unsupported JSON prompt format")
    else:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                prompts.append(stripped)
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("prompt"), str):
                prompts.append(parsed["prompt"].strip())
            elif isinstance(parsed, str):
                prompts.append(parsed.strip())

    unique_prompts = [p for p in dict.fromkeys(prompts) if p]
    if not unique_prompts:
        raise ValueError("No prompts loaded")
    return unique_prompts


def _extract_generated_files(events: List[Dict[str, Any]]) -> int:
    max_files = 0
    for event in events:
        message = str(event.get("message", ""))
        if "Generated " in message and " files." in message:
            try:
                middle = message.split("Generated ", 1)[1].split(" files.", 1)[0]
                value = int(middle.strip())
                if value > max_files:
                    max_files = value
            except Exception:
                continue
    return max_files


def run_case(
    *,
    api_base: str,
    prompt: str,
    model: str,
    timeout_sec: int,
    poll_interval_sec: float,
) -> CaseResult:
    start_payload = {"prompt": prompt, "selected_model": model}
    started = _http_json("POST", f"{api_base}/api/network/start", start_payload)
    run_id = str(started.get("run_id", ""))
    if not run_id:
        raise RuntimeError(f"Missing run_id in start response: {started}")

    started_at = time.time()
    terminal_state: Optional[Dict[str, Any]] = None
    status = "unknown"

    while True:
        state = _http_json("GET", f"{api_base}/api/network/runs/{run_id}")
        status = str(state.get("status", "unknown"))
        if status in {"completed", "failed"}:
            terminal_state = state
            break

        if (time.time() - started_at) > timeout_sec:
            status = "timeout"
            terminal_state = state
            break

        time.sleep(poll_interval_sec)

    duration_sec = round(time.time() - started_at, 2)
    events_payload = _http_json("GET", f"{api_base}/api/network/runs/{run_id}/events?limit=500")
    events = events_payload.get("events", [])
    if not isinstance(events, list):
        events = []

    files_generated = _extract_generated_files([e for e in events if isinstance(e, dict)])
    final_model = None
    if isinstance(terminal_state, dict):
        model_value = terminal_state.get("selected_model")
        if isinstance(model_value, str):
            final_model = model_value

    fallback_used = bool(final_model and final_model != model)
    error = None
    if status == "failed":
        for event in reversed(events):
            if isinstance(event, dict) and str(event.get("status")) == "failed":
                message = event.get("message")
                if isinstance(message, str) and message.strip():
                    error = message.strip()
                    break

    return CaseResult(
        prompt=prompt,
        run_id=run_id,
        requested_model=model,
        final_model=final_model,
        status=status,
        duration_sec=duration_sec,
        files_generated=files_generated,
        fallback_used=fallback_used,
        error=error,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate distributed network workflow quality.")
    parser.add_argument("--api-base", default="http://127.0.0.1", help="Base URL for nginx/backend")
    parser.add_argument("--model", default="gpt-4o-mini", help="Model to request")
    parser.add_argument("--prompts-file", default=None, help="JSON/JSONL/TXT prompts file")
    parser.add_argument("--timeout-sec", type=int, default=900, help="Max seconds per run")
    parser.add_argument("--poll-interval-sec", type=float, default=2.0, help="Polling interval")
    parser.add_argument("--output", default="backend/scripts/eval_report.json", help="Output report path")
    args = parser.parse_args()

    prompts = _load_prompts(args.prompts_file)
    results: List[CaseResult] = []

    print(f"[eval] prompts={len(prompts)} model={args.model} api={args.api_base}")
    for index, prompt in enumerate(prompts, start=1):
        print(f"[eval] case#{index} start")
        try:
            result = run_case(
                api_base=args.api_base.rstrip("/"),
                prompt=prompt,
                model=args.model,
                timeout_sec=max(30, args.timeout_sec),
                poll_interval_sec=max(0.5, args.poll_interval_sec),
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            result = CaseResult(
                prompt=prompt,
                run_id="",
                requested_model=args.model,
                final_model=None,
                status="http_error",
                duration_sec=0.0,
                files_generated=0,
                fallback_used=False,
                error=f"HTTP {exc.code}: {body}",
            )
        except Exception as exc:
            result = CaseResult(
                prompt=prompt,
                run_id="",
                requested_model=args.model,
                final_model=None,
                status="error",
                duration_sec=0.0,
                files_generated=0,
                fallback_used=False,
                error=str(exc),
            )

        results.append(result)
        print(
            "[eval] "
            f"case#{index} status={result.status} run_id={result.run_id or '-'} "
            f"duration={result.duration_sec}s files={result.files_generated} "
            f"model={result.final_model or result.requested_model}"
        )

    completed = sum(1 for r in results if r.status == "completed")
    failed = sum(1 for r in results if r.status == "failed")
    fallback_used = sum(1 for r in results if r.fallback_used)
    avg_duration = round(sum(r.duration_sec for r in results) / max(1, len(results)), 2)
    avg_files = round(sum(r.files_generated for r in results) / max(1, len(results)), 2)

    report = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api_base": args.api_base,
        "requested_model": args.model,
        "total_cases": len(results),
        "summary": {
            "completed": completed,
            "failed": failed,
            "fallback_used_cases": fallback_used,
            "avg_duration_sec": avg_duration,
            "avg_files_generated": avg_files,
        },
        "results": [
            {
                "prompt": r.prompt,
                "run_id": r.run_id,
                "requested_model": r.requested_model,
                "final_model": r.final_model,
                "status": r.status,
                "duration_sec": r.duration_sec,
                "files_generated": r.files_generated,
                "fallback_used": r.fallback_used,
                "error": r.error,
            }
            for r in results
        ],
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[eval] summary")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"[eval] report saved: {output_path}")


if __name__ == "__main__":
    main()
