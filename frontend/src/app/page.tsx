"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  Cpu,
  FileCode2,
  LayoutTemplate,
  Lightbulb,
  MessageCircle,
  PenTool,
  Play,
  RefreshCw,
  RotateCcw,
  Terminal,
} from "lucide-react";

interface WorkflowState {
  pm_spec: string | null;
  architecture_spec: string | null;
  current_logs: string | null;
  qa_report: string | null;
  source_code: Record<string, string> | null;
  github_deploy_log: string | null;
  total_tokens: number;
  total_cost: number;
  active_node: string | null;
  selected_model: string | null;
  error: string | null;
  status: string | null;
}

interface WorkflowMessage {
  active_node?: string;
  event_type?: string;
  from_role?: string | null;
  to_role?: string | null;
  pm_spec?: string | null;
  architecture_spec?: string | null;
  current_logs?: string | null;
  source_code?: Record<string, string> | null;
  qa_report?: string | null;
  github_deploy_log?: string | null;
  total_tokens?: number;
  total_cost?: number;
  status?: string;
  selected_model?: string | null;
  error?: string;
  message?: string;
  updated_at?: string;
}

interface AgentChatTurn {
  id: string;
  timeLabel: string;
  fromRole: string;
  toRole: string;
  message: string;
}

interface ModelCatalogResponse {
  available_models?: string[];
  default_model?: string;
  fallback_models?: string[];
}

interface QueueGroupInfo {
  exists: boolean;
  name: string;
  consumers: number;
  pending: number;
  lag: number;
  entries_read: number;
  last_delivered_id: string;
}

interface QueueConsumerInfo {
  name: string;
  pending: number;
  idle_ms: number;
}

interface QueueInfo {
  role: string;
  stream: string;
  stream_length: number;
  dlq_stream: string;
  dlq_length: number;
  group: QueueGroupInfo;
  consumers: QueueConsumerInfo[];
}

interface QueueStatsResponse {
  group_name?: string;
  roles?: string[];
  queues?: QueueInfo[];
}

interface DLQEntry {
  entry_id: string;
  payload: Record<string, unknown>;
}

interface DLQResponse {
  role?: string;
  count?: number;
  entries?: DLQEntry[];
}

interface DLQRequeueResponse {
  status?: string;
  run_id?: string;
  source_role?: string;
  target_role?: string;
  task_entry_id?: string;
  dlq_entry_id?: string;
  dlq_deleted?: boolean;
}

const FALLBACK_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-5-mini", "gpt-5"];
const NETWORK_ROLES = ["pm", "architect", "developer", "tool_execution", "qa", "github_deploy"] as const;
type NetworkRole = (typeof NETWORK_ROLES)[number];
type DashboardView = "user" | "operator";

const initialWorkflowState: WorkflowState = {
  pm_spec: null,
  architecture_spec: null,
  current_logs: null,
  qa_report: null,
  source_code: null,
  github_deploy_log: null,
  total_tokens: 0,
  total_cost: 0,
  active_node: null,
  selected_model: null,
  error: null,
  status: null,
};

const buildWsBaseUrl = (): string => {
  if (process.env.NEXT_PUBLIC_WS_BASE_URL) {
    return process.env.NEXT_PUBLIC_WS_BASE_URL.replace(/\/$/, "");
  }

  if (typeof window === "undefined") {
    return "";
  }

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}`;
};

const buildApiBaseUrl = (): string => {
  if (process.env.NEXT_PUBLIC_API_BASE_URL) {
    return process.env.NEXT_PUBLIC_API_BASE_URL.replace(/\/$/, "");
  }

  if (typeof window === "undefined") {
    return "";
  }

  return `${window.location.protocol}//${window.location.host}`;
};

const asRecord = (value: unknown): Record<string, unknown> => {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
};

const asString = (value: unknown, fallback = "-"): string => {
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  return fallback;
};

const asNumber = (value: unknown, fallback = 0): number => {
  const parsed = Number(value);
  if (Number.isFinite(parsed)) {
    return parsed;
  }
  return fallback;
};

const summarizeDlqEntry = (entry: DLQEntry) => {
  const payload = asRecord(entry.payload);
  const task = asRecord(payload.task);

  return {
    runId: asString(task.run_id ?? payload.run_id, "-"),
    error: asString(payload.error ?? task.last_error, "-"),
    attempt: asNumber(payload.attempt ?? task._attempt, 1),
    maxAttempts: asNumber(payload.max_attempts, asNumber(payload.attempt ?? task._attempt, 1)),
    failedAt: asString(payload.failed_at, "-"),
  };
};

const extractErrorMessage = async (response: Response): Promise<string> => {
  try {
    const payload = (await response.json()) as { detail?: string; error?: string };
    if (payload?.detail) return payload.detail;
    if (payload?.error) return payload.error;
  } catch {
    // ignore json parse errors
  }
  return `Request failed with status ${response.status}`;
};

export default function Dashboard() {
  const [prompt, setPrompt] = useState("");
  const [selectedModel, setSelectedModel] = useState(FALLBACK_MODELS[0]);
  const [modelOptions, setModelOptions] = useState<string[]>(FALLBACK_MODELS);
  const [fallbackModelOptions, setFallbackModelOptions] = useState<string[]>([]);
  const [requestedModelForRun, setRequestedModelForRun] = useState<string | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [currentClientId, setCurrentClientId] = useState<string | null>(null);
  const [activeAgent, setActiveAgent] = useState<string>("idle");
  const [eventFeed, setEventFeed] = useState<string[]>(["System initialized. Awaiting a new request."]);
  const [agentChats, setAgentChats] = useState<AgentChatTurn[]>([]);
  const [dashboardView, setDashboardView] = useState<DashboardView>("user");
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [lastUpdateAt, setLastUpdateAt] = useState<string | null>(null);
  const [workflowState, setWorkflowState] = useState<WorkflowState>(initialWorkflowState);
  const [queueGroupName, setQueueGroupName] = useState<string>("network-workers");
  const [queueInfos, setQueueInfos] = useState<QueueInfo[]>([]);
  const [adminRole, setAdminRole] = useState<NetworkRole>("pm");
  const [dlqEntries, setDlqEntries] = useState<DLQEntry[]>([]);
  const [redriveTargetRole, setRedriveTargetRole] = useState<NetworkRole>("pm");
  const [isQueueLoading, setIsQueueLoading] = useState(false);
  const [isDlqLoading, setIsDlqLoading] = useState(false);
  const [redriveEntryId, setRedriveEntryId] = useState<string | null>(null);
  const [adminError, setAdminError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const lastNodeRef = useRef<string | null>(null);
  const lastModelRef = useRef<string | null>(null);
  const agentChatSeenRef = useRef<Set<string>>(new Set());
  const chatScrollRef = useRef<HTMLDivElement | null>(null);

  const addEvent = (message: string) => {
    const ts = new Date().toLocaleTimeString("ko-KR", { hour12: false });
    setEventFeed((prev) => [...prev.slice(-199), `[${ts}] ${message}`]);
  };

  const closeCurrentSocket = () => {
    const socket = wsRef.current;
    if (!socket) return;

    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;

    if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
      socket.close();
    }
    wsRef.current = null;
  };

  const mapActiveAgent = (activeNode: string): string => {
    if (activeNode.includes("pm")) return "pm";
    if (activeNode.includes("architect")) return "architect";
    if (activeNode.includes("developer") || activeNode.includes("tool")) return "developer";
    if (activeNode.includes("qa")) return "qa";
    if (activeNode.includes("github_deploy")) return "deploy";
    if (activeNode === "END") return "done";
    return "idle";
  };

  const toTimeLabel = (isoTime?: string) => {
    if (!isoTime) {
      return new Date().toLocaleTimeString("ko-KR", { hour12: false });
    }
    const parsed = new Date(isoTime);
    if (Number.isNaN(parsed.getTime())) {
      return new Date().toLocaleTimeString("ko-KR", { hour12: false });
    }
    return parsed.toLocaleTimeString("ko-KR", { hour12: false });
  };

  const getRoleTone = (role: string) => {
    if (role === "pm") return "border-amber-400/40 bg-amber-100/60 text-amber-800";
    if (role === "architect") return "border-sky-400/40 bg-sky-100/60 text-sky-800";
    if (role === "developer") return "border-teal-500/40 bg-teal-100/60 text-teal-800";
    if (role === "tool_execution") return "border-cyan-500/40 bg-cyan-100/60 text-cyan-800";
    if (role === "qa") return "border-rose-400/40 bg-rose-100/60 text-rose-800";
    if (role === "github_deploy") return "border-indigo-400/40 bg-indigo-100/60 text-indigo-800";
    return "border-[color:var(--line)] bg-[#f8f2e5] text-[color:var(--text)]";
  };

  useEffect(() => {
    const el = chatScrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [agentChats]);

  useEffect(() => {
    const files = workflowState.source_code ? Object.keys(workflowState.source_code).sort() : [];
    if (files.length === 0) {
      setSelectedFile(null);
      return;
    }

    if (!selectedFile || !workflowState.source_code?.[selectedFile]) {
      setSelectedFile(files[0]);
    }
  }, [workflowState.source_code, selectedFile]);

  useEffect(() => {
    return () => {
      closeCurrentSocket();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const loadModelCatalog = async () => {
      try {
        const response = await fetch(`${buildApiBaseUrl()}/api/models`);
        if (!response.ok) {
          throw new Error(`Failed to fetch model catalog: ${response.status}`);
        }

        const payload = (await response.json()) as ModelCatalogResponse;
        const availableModels = Array.isArray(payload.available_models) && payload.available_models.length > 0
          ? payload.available_models
          : FALLBACK_MODELS;
        const fallbackModels = Array.isArray(payload.fallback_models)
          ? payload.fallback_models.filter((modelName) => availableModels.includes(modelName))
          : [];
        const defaultModel = payload.default_model && availableModels.includes(payload.default_model)
          ? payload.default_model
          : availableModels[0];

        if (cancelled) return;
        setModelOptions(availableModels);
        setFallbackModelOptions(fallbackModels);
        setSelectedModel((prev) => (availableModels.includes(prev) ? prev : defaultModel));
      } catch {
        if (cancelled) return;
        setModelOptions(FALLBACK_MODELS);
        setFallbackModelOptions([]);
        setSelectedModel((prev) => (FALLBACK_MODELS.includes(prev) ? prev : FALLBACK_MODELS[0]));
      }
    };

    void loadModelCatalog();
    return () => {
      cancelled = true;
    };
  }, []);

  const loadQueueInfos = useCallback(async (showSpinner = false) => {
    if (showSpinner) setIsQueueLoading(true);
    try {
      const response = await fetch(`${buildApiBaseUrl()}/api/network/admin/queues`);
      if (!response.ok) {
        throw new Error(await extractErrorMessage(response));
      }

      const payload = (await response.json()) as QueueStatsResponse;
      const queues = Array.isArray(payload.queues) ? payload.queues : [];
      const groupName = typeof payload.group_name === "string" && payload.group_name
        ? payload.group_name
        : "network-workers";

      setQueueInfos(queues);
      setQueueGroupName(groupName);
      setAdminError(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to load queue status";
      setAdminError(message);
    } finally {
      if (showSpinner) setIsQueueLoading(false);
    }
  }, []);

  const loadDlqEntries = useCallback(async (role: NetworkRole, showSpinner = false) => {
    if (showSpinner) setIsDlqLoading(true);
    try {
      const response = await fetch(`${buildApiBaseUrl()}/api/network/admin/dlq/${role}?limit=100`);
      if (!response.ok) {
        throw new Error(await extractErrorMessage(response));
      }

      const payload = (await response.json()) as DLQResponse;
      const entries = Array.isArray(payload.entries) ? payload.entries : [];
      setDlqEntries(entries);
      setAdminError(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to load DLQ entries";
      setAdminError(message);
    } finally {
      if (showSpinner) setIsDlqLoading(false);
    }
  }, []);

  const refreshAdminPanel = useCallback(async (showSpinner = false) => {
    await Promise.all([
      loadQueueInfos(showSpinner),
      loadDlqEntries(adminRole, showSpinner),
    ]);
  }, [adminRole, loadDlqEntries, loadQueueInfos]);

  useEffect(() => {
    setRedriveTargetRole(adminRole);
    if (dashboardView === "operator") {
      void refreshAdminPanel(true);
    }
  }, [adminRole, dashboardView, refreshAdminPanel]);

  useEffect(() => {
    if (dashboardView !== "operator") {
      return;
    }
    const intervalMs = isRunning ? 5000 : 15000;
    const timer = window.setInterval(() => {
      void refreshAdminPanel(false);
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [dashboardView, isRunning, refreshAdminPanel]);

  const handleRequeueDlqEntry = async (entryId: string) => {
    setRedriveEntryId(entryId);
    try {
      const response = await fetch(`${buildApiBaseUrl()}/api/network/admin/dlq/${adminRole}/requeue`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          entry_id: entryId,
          target_role: redriveTargetRole,
          reset_attempt: true,
          delete_from_dlq: true,
        }),
      });

      if (!response.ok) {
        throw new Error(await extractErrorMessage(response));
      }

      const payload = (await response.json()) as DLQRequeueResponse;
      const runId = payload.run_id ?? "-";
      const targetRole = payload.target_role ?? redriveTargetRole;
      addEvent(`[DLQ] Requeued ${entryId} -> ${targetRole} (run=${runId})`);
      await refreshAdminPanel(true);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to requeue DLQ entry";
      setAdminError(message);
      addEvent(`[ERROR] DLQ requeue failed: ${message}`);
    } finally {
      setRedriveEntryId(null);
    }
  };

  const handleWorkflowMessage = (raw: WorkflowMessage) => {
    if (raw.error) {
      setWorkflowState((prev) => ({ ...prev, error: raw.error ?? "Unknown workflow error" }));
      addEvent(`[ERROR] ${raw.error}`);
      setIsRunning(false);
      return;
    }

    const patch: Partial<WorkflowState> = {};
    if (raw.pm_spec !== undefined) patch.pm_spec = raw.pm_spec ?? null;
    if (raw.architecture_spec !== undefined) patch.architecture_spec = raw.architecture_spec ?? null;
    if (raw.current_logs !== undefined) patch.current_logs = raw.current_logs ?? null;
    if (raw.source_code !== undefined) patch.source_code = raw.source_code ?? null;
    if (raw.qa_report !== undefined) patch.qa_report = raw.qa_report ?? null;
    if (raw.github_deploy_log !== undefined) patch.github_deploy_log = raw.github_deploy_log ?? null;
    if (raw.total_tokens !== undefined) patch.total_tokens = raw.total_tokens;
    if (raw.total_cost !== undefined) patch.total_cost = raw.total_cost;
    if (raw.active_node !== undefined) patch.active_node = raw.active_node;
    if (raw.status !== undefined) patch.status = raw.status;
    if (raw.selected_model !== undefined) patch.selected_model = raw.selected_model ?? null;

    if (Object.keys(patch).length > 0) {
      setWorkflowState((prev) => ({ ...prev, ...patch }));
      setLastUpdateAt(new Date().toLocaleTimeString("ko-KR", { hour12: false }));
    }

    if (raw.selected_model && raw.selected_model !== lastModelRef.current) {
      const previousModel = lastModelRef.current;
      lastModelRef.current = raw.selected_model;
      if (requestedModelForRun && raw.selected_model !== requestedModelForRun) {
        addEvent(`[MODEL] Fallback applied: ${requestedModelForRun} -> ${raw.selected_model}`);
      } else if (previousModel && previousModel !== raw.selected_model) {
        addEvent(`[MODEL] Active model changed: ${previousModel} -> ${raw.selected_model}`);
      } else {
        addEvent(`[MODEL] Active model: ${raw.selected_model}`);
      }
    }

    const messageText = raw.message;
    if (raw.event_type === "agent_chat" && messageText && raw.from_role) {
      const fromRole = raw.from_role;
      const toRole = raw.to_role ?? "supervisor";
      const dedupeKey = `${raw.updated_at ?? ""}|${fromRole}|${toRole}|${messageText}`;
      if (!agentChatSeenRef.current.has(dedupeKey)) {
        agentChatSeenRef.current.add(dedupeKey);
        setAgentChats((prev) => [
          ...prev.slice(-149),
          {
            id: dedupeKey,
            timeLabel: toTimeLabel(raw.updated_at),
            fromRole,
            toRole,
            message: messageText,
          },
        ]);
      }
    }

    if (raw.message) {
      if (raw.event_type === "agent_chat") {
        // agent_chat is rendered in the dedicated timeline panel.
      } else if (raw.from_role) {
        addEvent(`[${raw.from_role}] ${raw.message}`);
      } else {
        addEvent(raw.message);
      }
    }

    if (raw.active_node && raw.active_node !== lastNodeRef.current) {
      lastNodeRef.current = raw.active_node;
      addEvent(`Node switched: ${raw.active_node}`);
      setActiveAgent(mapActiveAgent(raw.active_node));
    }

    if (raw.status === "completed") {
      setActiveAgent("done");
      setIsRunning(false);
      addEvent("Workflow completed.");
      return;
    }

    if (raw.status === "failed") {
      setIsRunning(false);
      setWorkflowState((prev) => ({ ...prev, error: raw.message ?? prev.error ?? "Workflow failed" }));
      addEvent(`Workflow failed.${raw.message ? ` ${raw.message}` : ""}`);
      return;
    }

    if (raw.status && raw.status !== "completed") {
      addEvent(`Status: ${raw.status}`);
    }
  };

  const connectNetworkWorkflow = (runId: string) => {
    closeCurrentSocket();
    const socket = new WebSocket(`${buildWsBaseUrl()}/ws/network/${runId}`);
    wsRef.current = socket;

    socket.onopen = () => {
      setIsConnected(true);
    };

    socket.onclose = () => {
      setIsConnected(false);
      setIsRunning(false);
    };

    socket.onerror = () => {
      setIsConnected(false);
      setWorkflowState((prev) => ({
        ...prev,
        error: "Network workflow WebSocket 연결 오류가 발생했습니다.",
      }));
      addEvent("[ERROR] Network workflow socket error.");
    };

    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as WorkflowMessage;
        handleWorkflowMessage(data);
      } catch {
        addEvent("[ERROR] Failed to parse network workflow payload.");
      }
    };
  };

  const handleStartWorkflow = async () => {
    const trimmedPrompt = prompt.trim();
    if (!trimmedPrompt) return;

    lastNodeRef.current = null;
    setWorkflowState({ ...initialWorkflowState, selected_model: selectedModel });
    setRequestedModelForRun(selectedModel);
    lastModelRef.current = selectedModel;
    agentChatSeenRef.current.clear();
    setAgentChats([]);
    setActiveAgent("pm");
    setIsRunning(true);

    try {
      const response = await fetch(`${buildApiBaseUrl()}/api/network/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: trimmedPrompt,
          selected_model: selectedModel,
        }),
      });

      if (!response.ok) {
        throw new Error(`Network start failed with status ${response.status}`);
      }

      const payload = (await response.json()) as { run_id: string; selected_model?: string };
      const runId = payload.run_id;
      const resolvedModel = payload.selected_model ?? selectedModel;
      setCurrentClientId(runId);
      setWorkflowState((prev) => ({ ...prev, selected_model: resolvedModel }));
      lastModelRef.current = resolvedModel;
      setEventFeed([
        `[${new Date().toLocaleTimeString("ko-KR", { hour12: false })}] Network session created: ${runId} (requested=${selectedModel}, active=${resolvedModel})`,
      ]);
      connectNetworkWorkflow(runId);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown network start error";
      setIsRunning(false);
      setWorkflowState((prev) => ({ ...prev, error: message }));
      addEvent(`[ERROR] ${message}`);
    }
  };

  const agents = [
    { id: "pm", name: "PM", description: "요구사항 분석", icon: Lightbulb },
    { id: "architect", name: "Architect", description: "구조 설계", icon: LayoutTemplate },
    { id: "developer", name: "Developer", description: "구현 / 실행", icon: PenTool },
    { id: "qa", name: "QA", description: "검증 / 피드백", icon: Activity },
    { id: "deploy", name: "Deploy", description: "배포 단계", icon: Cpu },
  ];

  const stages = ["pm", "architect", "developer", "qa", "deploy", "done"];
  const stageIndex = Math.max(stages.indexOf(activeAgent), 0);
  const progressPercent = activeAgent === "idle" ? 0 : Math.round(((stageIndex + 1) / stages.length) * 100);

  const sortedFiles = workflowState.source_code ? Object.keys(workflowState.source_code).sort() : [];
  const selectedCode = selectedFile && workflowState.source_code ? workflowState.source_code[selectedFile] : "";
  const requestedModelLabel = requestedModelForRun ?? selectedModel;
  const activeModelLabel = workflowState.selected_model ?? selectedModel;
  const isFallbackActive = requestedModelForRun !== null && requestedModelLabel !== activeModelLabel;
  const fallbackChain = [selectedModel, ...fallbackModelOptions.filter((modelName) => modelName !== selectedModel)].join(" -> ");
  const roleIndex = new Map(NETWORK_ROLES.map((role, index) => [role, index]));
  const orderedQueueInfos = [...queueInfos].sort((a, b) => {
    const aIndex = roleIndex.get(a.role as NetworkRole) ?? Number.MAX_SAFE_INTEGER;
    const bIndex = roleIndex.get(b.role as NetworkRole) ?? Number.MAX_SAFE_INTEGER;
    return aIndex - bIndex;
  });

  const pendingTotal = orderedQueueInfos.reduce((acc, item) => acc + item.group.pending, 0);
  const dlqTotal = orderedQueueInfos.reduce((acc, item) => acc + item.dlq_length, 0);
  const consumerTotal = orderedQueueInfos.reduce((acc, item) => acc + item.group.consumers, 0);

  const statusLabel = isRunning ? (isConnected ? "Live session" : "Connecting") : "Standby";
  const statusClass = isRunning ? (isConnected ? "tag-teal" : "tag-amber") : "tag-rose";
  const statusDotClass = isRunning ? (isConnected ? "status-live" : "status-wait") : "status-idle";

  return (
    <div className="app-shell">
      <div className="app-container space-y-5 md:space-y-6">
        <section className="panel panel-elevated p-5 md:p-7 reveal" style={{ animationDelay: "50ms" }}>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="max-w-4xl">
              <p className="panel-title">Distributed Engineering Cockpit</p>
              <h1 className="mt-2 text-2xl md:text-4xl font-semibold tracking-tight leading-tight">
                AI Orchestrator Control Center
              </h1>
              <p className="mt-3 text-sm md:text-[15px] text-[color:var(--text-muted)]">
                네트워크 기반 멀티 에이전트 워크플로우를 실시간으로 시작, 추적, 복구하는 운영형 UI입니다.
                모델 라우팅, 실행 이벤트, 큐 상태, DLQ Redrive를 한 화면에서 관리할 수 있습니다.
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <span className={`tag ${statusClass}`}>
                <span className={`status-dot ${statusDotClass}`} />
                {statusLabel}
              </span>
              <span className="tag tag-teal mono">
                run {currentClientId ? currentClientId.slice(0, 12) : "not-started"}
              </span>
              <span className="tag tag-amber">Network Chat PoC</span>
            </div>
          </div>

          <div className="mt-5 grid gap-3 lg:grid-cols-[1fr_220px_180px]">
            <textarea
              rows={3}
              placeholder="예: 사용자 요구사항을 입력하면 PM→Architect→Developer→QA→Deploy 순으로 자동 개발 플로우를 실행합니다."
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              disabled={isRunning}
              className="panel bg-[color:var(--surface-soft)] border-[color:var(--line)] p-3 text-sm resize-none outline-none focus:ring-2 focus:ring-teal-600/30 disabled:opacity-70"
            />

            <select
              value={selectedModel}
              onChange={(event) => setSelectedModel(event.target.value)}
              disabled={isRunning}
              className="panel bg-[color:var(--surface-soft)] border-[color:var(--line)] px-3 py-3 text-sm outline-none focus:ring-2 focus:ring-teal-600/30 disabled:opacity-70"
            >
              {modelOptions.map((modelName) => (
                <option key={modelName} value={modelName}>
                  {modelName}
                </option>
              ))}
            </select>

            <button
              type="button"
              onClick={handleStartWorkflow}
              disabled={!prompt.trim() || isRunning}
              className="rounded-xl border border-teal-700/40 bg-teal-700 text-white px-4 py-3 text-sm font-semibold inline-flex items-center justify-center gap-2 disabled:opacity-55 transition-colors hover:bg-teal-600"
            >
              <Play className="w-4 h-4" />
              {isRunning ? "Running..." : "Start Run"}
            </button>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
            <span className="tag tag-amber mono">fallback {fallbackChain}</span>
            <span className="tag tag-teal mono">requested {requestedModelLabel}</span>
            <span className={`tag ${isFallbackActive ? "tag-amber" : "tag-teal"} mono`}>
              active {activeModelLabel}
            </span>
            <span className="tag tag-rose mono">updated {lastUpdateAt ?? "-"}</span>
          </div>

          <div className="mt-4 inline-flex items-center gap-1 rounded-md border border-[color:var(--line)] bg-[color:var(--surface-soft)] p-1">
            <button
              type="button"
              onClick={() => setDashboardView("user")}
              className={`px-3 py-1 text-xs font-semibold rounded-md transition-colors ${
                dashboardView === "user"
                  ? "bg-teal-700 text-white"
                  : "text-[color:var(--text-muted)] hover:bg-[#efe8d8]"
              }`}
            >
              사용자용 (실행/대화)
            </button>
            <button
              type="button"
              onClick={() => setDashboardView("operator")}
              className={`px-3 py-1 text-xs font-semibold rounded-md transition-colors ${
                dashboardView === "operator"
                  ? "bg-teal-700 text-white"
                  : "text-[color:var(--text-muted)] hover:bg-[#efe8d8]"
              }`}
            >
              운영자용 (큐/DLQ)
            </button>
          </div>
        </section>

        {dashboardView === "user" ? (
          <section className="grid grid-cols-2 xl:grid-cols-4 gap-3 reveal" style={{ animationDelay: "100ms" }}>
            <div className="panel p-4">
              <p className="panel-title">Session</p>
              <p className="mt-2 mono text-xs break-all">{currentClientId ?? "Not started"}</p>
            </div>
            <div className="panel p-4">
              <p className="panel-title">Active Node</p>
              <p className="mt-2 text-sm font-semibold">{workflowState.active_node ?? "idle"}</p>
            </div>
            <div className="panel p-4">
              <p className="panel-title">Tokens</p>
              <p className="mt-2 text-lg font-semibold">{workflowState.total_tokens.toLocaleString()}</p>
            </div>
            <div className="panel p-4">
              <p className="panel-title">Cost</p>
              <p className="mt-2 text-lg font-semibold">${workflowState.total_cost.toFixed(4)}</p>
            </div>
          </section>
        ) : (
          <section className="grid grid-cols-2 xl:grid-cols-4 gap-3 reveal" style={{ animationDelay: "100ms" }}>
            <div className="panel p-4">
              <p className="panel-title">Queue Group</p>
              <p className="mt-2 mono text-xs break-all">{queueGroupName}</p>
            </div>
            <div className="panel p-4">
              <p className="panel-title">Consumers</p>
              <p className="mt-2 text-lg font-semibold">{consumerTotal}</p>
            </div>
            <div className="panel p-4">
              <p className="panel-title">Queue / DLQ</p>
              <p className="mt-2 text-sm font-semibold">{pendingTotal} / {dlqTotal}</p>
            </div>
            <div className="panel p-4">
              <p className="panel-title">Admin Role</p>
              <p className="mt-2 text-sm font-semibold">{adminRole}</p>
            </div>
          </section>
        )}

        {dashboardView === "user" && (
          <>
            <section className="panel p-4 md:p-5 reveal" style={{ animationDelay: "150ms" }}>
          <div className="flex items-center justify-between gap-4">
            <p className="panel-title">Pipeline Progress</p>
            <span className="mono text-xs text-[color:var(--text-muted)]">{progressPercent}%</span>
          </div>
          <div className="mt-3 h-2 rounded-full bg-[color:var(--line)] overflow-hidden">
            <div
              className="h-full rounded-full bg-gradient-to-r from-teal-600 via-teal-500 to-amber-500 transition-all duration-500"
              style={{ width: `${progressPercent}%` }}
            />
          </div>

          <div className="mt-4 grid gap-2 md:grid-cols-5">
            {agents.map((agent) => (
              <div
                key={agent.id}
                className={`rounded-xl border p-3 transition-all ${
                  activeAgent === agent.id
                    ? "border-teal-600/50 bg-teal-100/55"
                    : "border-[color:var(--line)] bg-[color:var(--surface-soft)]"
                }`}
              >
                <div className="flex items-center gap-2">
                  <agent.icon className="w-4 h-4" />
                  <span className="text-sm font-semibold">{agent.name}</span>
                </div>
                <p className="mt-1 text-xs text-[color:var(--text-muted)]">{agent.description}</p>
              </div>
            ))}
          </div>
            </section>

            <section className="grid gap-5 lg:grid-cols-[0.62fr_1.38fr] reveal" style={{ animationDelay: "200ms" }}>
          <div className="panel p-4 md:p-5 min-h-[460px]">
            <div className="flex items-center gap-2 pb-3 border-b border-[color:var(--line)]">
              <Terminal className="w-4 h-4" />
              <p className="panel-title">PM & Architect Documents</p>
            </div>
            <div className="mt-3 h-[380px] overflow-y-auto custom-scrollbar rounded-xl border border-[color:var(--line)] bg-[color:var(--surface-soft)] p-3">
              <pre className="whitespace-pre-wrap text-xs md:text-sm mono text-[color:var(--text)]">
                {workflowState.pm_spec ? workflowState.pm_spec : "No PM spec generated.\n"}
                {"\n"}
                {workflowState.architecture_spec ? workflowState.architecture_spec : ""}
              </pre>
            </div>
          </div>

          <div className="panel p-4 md:p-5 min-h-[460px]">
            <div className="flex items-center gap-2 pb-3 border-b border-[color:var(--line)]">
              <Activity className="w-4 h-4" />
              <p className="panel-title">Runtime Feed</p>
            </div>
            <div className="mt-3 grid gap-3">
              <div className="rounded-xl border border-[color:var(--line)] bg-[color:var(--surface-soft)] p-3 h-[160px] overflow-y-auto custom-scrollbar" ref={chatScrollRef}>
                <div className="flex items-center justify-between gap-2 mb-2">
                  <div className="flex items-center gap-1">
                    <MessageCircle className="w-3 h-3" />
                    <p className="text-[11px] text-[color:var(--text-muted)] font-semibold">Agent Chat Timeline</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      agentChatSeenRef.current.clear();
                      setAgentChats([]);
                    }}
                    className="rounded-md border border-[color:var(--line)] bg-white px-2 py-0.5 text-[10px] mono hover:bg-[#efe8d8]"
                  >
                    clear
                  </button>
                </div>
                {agentChats.length === 0 ? (
                  <p className="text-[11px] mono text-[color:var(--text-muted)]">No agent chat yet.</p>
                ) : (
                  <div className="space-y-2">
                    {agentChats.map((turn) => (
                      <div key={turn.id} className={`rounded-lg border p-2 ${getRoleTone(turn.fromRole)}`}>
                        <p className="mono text-[10px] opacity-75">
                          {turn.timeLabel} {turn.fromRole} -&gt; {turn.toRole}
                        </p>
                        <p className="mt-1 text-[11px] whitespace-pre-wrap leading-relaxed">{turn.message}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
              <div className="rounded-xl border border-[color:var(--line)] bg-[color:var(--surface-soft)] p-3 h-[130px] overflow-y-auto custom-scrollbar">
                <p className="text-[11px] text-[color:var(--text-muted)] font-semibold mb-2">Events</p>
                <pre className="whitespace-pre-wrap text-[11px] mono">
                  {eventFeed.length ? eventFeed.join("\n") : "No events yet."}
                </pre>
              </div>
              <div className="rounded-xl border border-[color:var(--line)] bg-[color:var(--surface-soft)] p-3 h-[130px] overflow-y-auto custom-scrollbar">
                <p className="text-[11px] text-[color:var(--text-muted)] font-semibold mb-2">Execution / QA / Deploy</p>
                <pre className="whitespace-pre-wrap text-[11px] mono">
                  {workflowState.current_logs ? workflowState.current_logs : "No execution logs yet."}
                  {workflowState.qa_report ? `\n\n${workflowState.qa_report}` : ""}
                  {workflowState.github_deploy_log ? `\n\n${workflowState.github_deploy_log}` : ""}
                  {workflowState.error ? `\n\n[ERROR] ${workflowState.error}` : ""}
                </pre>
              </div>
            </div>
          </div>
            </section>

            <section className="panel p-4 md:p-5 min-h-[460px] reveal" style={{ animationDelay: "240ms" }}>
              <div className="flex items-center gap-2 pb-3 border-b border-[color:var(--line)]">
                <FileCode2 className="w-4 h-4" />
                <p className="panel-title">Source Explorer</p>
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-[180px_1fr] h-[380px]">
                <div className="rounded-xl border border-[color:var(--line)] bg-[color:var(--surface-soft)] overflow-y-auto custom-scrollbar">
                  {sortedFiles.length === 0 ? (
                    <div className="p-3 text-xs text-[color:var(--text-muted)]">No generated files.</div>
                  ) : (
                    sortedFiles.map((path) => (
                      <button
                        key={path}
                        type="button"
                        onClick={() => setSelectedFile(path)}
                        className={`w-full text-left px-3 py-2 text-xs mono border-b border-[color:var(--line)] transition-colors ${
                          selectedFile === path
                            ? "bg-teal-100 text-teal-800"
                            : "text-[color:var(--text)] hover:bg-[#efe8d8]"
                        }`}
                      >
                        {path}
                      </button>
                    ))
                  )}
                </div>
                <div className="rounded-xl border border-[color:var(--line)] overflow-auto custom-scrollbar soft-code">
                  <pre className="p-3 text-[11px] whitespace-pre-wrap mono">
                    {selectedCode || "Select a file to inspect generated code."}
                  </pre>
                </div>
              </div>
            </section>
          </>
        )}

        {dashboardView === "operator" && (
          <section className="grid gap-5 xl:grid-cols-12 reveal" style={{ animationDelay: "260ms" }}>
          <div className="panel xl:col-span-7 p-4 md:p-5">
            <div className="flex items-center justify-between gap-3 pb-3 border-b border-[color:var(--line)]">
              <div>
                <p className="panel-title">Queue / Consumer Group</p>
                <p className="text-xs text-[color:var(--text-muted)] mt-1">
                  Group <span className="mono">{queueGroupName}</span>
                </p>
              </div>
              <button
                type="button"
                onClick={() => { void refreshAdminPanel(true); }}
                disabled={isQueueLoading || isDlqLoading}
                className="inline-flex items-center gap-2 rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-3 py-2 text-xs font-semibold disabled:opacity-60 hover:bg-[#ece4d4]"
              >
                <RefreshCw className={`w-3 h-3 ${isQueueLoading || isDlqLoading ? "animate-spin" : ""}`} />
                Refresh
              </button>
            </div>

            {adminError && (
              <div className="mt-3 rounded-lg border border-rose-400/40 bg-rose-100 text-rose-700 p-2 text-xs">
                {adminError}
              </div>
            )}

            <div className="mt-3 grid gap-3 md:grid-cols-2">
              {orderedQueueInfos.length === 0 && (
                <div className="rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] p-3 text-xs text-[color:var(--text-muted)]">
                  No queue metrics available.
                </div>
              )}
              {orderedQueueInfos.map((queueInfo) => (
                <div key={queueInfo.role} className="rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-semibold">{queueInfo.role}</span>
                    <span className="tag tag-teal">pending {queueInfo.group.pending}</span>
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[color:var(--text-muted)]">
                    <div>stream {queueInfo.stream_length}</div>
                    <div>lag {queueInfo.group.lag}</div>
                    <div>consumers {queueInfo.group.consumers}</div>
                    <div>dlq {queueInfo.dlq_length}</div>
                  </div>
                  <p className="mt-2 text-[11px] mono text-[color:var(--text-muted)] truncate">{queueInfo.stream}</p>
                </div>
              ))}
            </div>
          </div>

          <div className="panel xl:col-span-5 p-4 md:p-5">
            <div className="pb-3 border-b border-[color:var(--line)]">
              <p className="panel-title">DLQ Redrive</p>
            </div>

            <div className="mt-3 grid grid-cols-2 gap-3">
              <label className="text-[11px] text-[color:var(--text-muted)]">
                Source role
                <select
                  value={adminRole}
                  onChange={(event) => setAdminRole(event.target.value as NetworkRole)}
                  className="mt-1 w-full rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-2 py-2 text-xs"
                >
                  {NETWORK_ROLES.map((roleName) => (
                    <option key={roleName} value={roleName}>
                      {roleName}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-[11px] text-[color:var(--text-muted)]">
                Target role
                <select
                  value={redriveTargetRole}
                  onChange={(event) => setRedriveTargetRole(event.target.value as NetworkRole)}
                  className="mt-1 w-full rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-2 py-2 text-xs"
                >
                  {NETWORK_ROLES.map((roleName) => (
                    <option key={roleName} value={roleName}>
                      {roleName}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <div className="mt-3 space-y-3 max-h-[420px] overflow-y-auto custom-scrollbar pr-1">
              {isDlqLoading && dlqEntries.length === 0 && (
                <div className="text-xs text-[color:var(--text-muted)]">Loading DLQ entries...</div>
              )}
              {!isDlqLoading && dlqEntries.length === 0 && (
                <div className="text-xs text-[color:var(--text-muted)]">No DLQ entries for this role.</div>
              )}

              {dlqEntries.map((entry) => {
                const summary = summarizeDlqEntry(entry);
                return (
                  <div key={entry.entry_id} className="rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] p-3">
                    <p className="mono text-[11px] break-all text-[color:var(--text-muted)]">{entry.entry_id}</p>
                    <p className="mt-2 text-xs">run {summary.runId}</p>
                    <p className="text-xs text-[color:var(--text-muted)]">attempt {summary.attempt}/{summary.maxAttempts}</p>
                    <p className="text-xs text-[color:var(--text-muted)]">failed_at {summary.failedAt}</p>
                    <p className="mt-2 text-xs text-rose-700 whitespace-pre-wrap break-words">{summary.error}</p>
                    <button
                      type="button"
                      onClick={() => { void handleRequeueDlqEntry(entry.entry_id); }}
                      disabled={redriveEntryId === entry.entry_id}
                      className="mt-3 inline-flex items-center gap-2 rounded-lg border border-teal-700/35 bg-teal-700 text-white px-3 py-1.5 text-xs font-semibold disabled:opacity-60 hover:bg-teal-600"
                    >
                      <RotateCcw className={`w-3 h-3 ${redriveEntryId === entry.entry_id ? "animate-spin" : ""}`} />
                      {redriveEntryId === entry.entry_id ? "Requeueing..." : "Requeue"}
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
          </section>
        )}
      </div>
    </div>
  );
}
