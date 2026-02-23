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
  approval_stage: string | null;
  approval_requested_at: string | null;
  approval_pending_next_roles: string[];
  approval_last_action: string | null;
  approval_last_stage: string | null;
  approval_last_actor: string | null;
  approval_last_note: string | null;
  approval_last_decision_at: string | null;
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
  approval_stage?: string | null;
  approval_requested_at?: string | null;
  approval_pending_next_roles?: string[];
  approval_last_action?: string | null;
  approval_last_stage?: string | null;
  approval_last_actor?: string | null;
  approval_last_note?: string | null;
  approval_last_decision_at?: string | null;
}

interface RuntimeTimelineItem {
  id: string;
  timeLabel: string;
  category: "chat" | "event";
  fromRole?: string;
  toRole?: string;
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

interface ApprovalDecisionResponse {
  run_id?: string;
  action?: string;
  status?: string;
  next_role?: string;
  next_roles?: string[];
  task_entry_id?: string;
}

interface RequeueCandidate {
  entryId: string;
  runId: string;
  attemptLabel: string;
}

const FALLBACK_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-5-mini", "gpt-5"];
const NETWORK_ROLES = [
  "pm",
  "architect",
  "developer_backend",
  "developer_frontend",
  "developer",
  "tool_execution",
  "qa",
  "github_deploy",
] as const;
type NetworkRole = (typeof NETWORK_ROLES)[number];
const NETWORK_ROLE_SET = new Set<string>(NETWORK_ROLES);
type RuntimeRoleFilter = "all" | "event" | NetworkRole;
const WS_RECONNECT_DEFAULT_MAX_ATTEMPTS = 5;
const WS_RECONNECT_DEFAULT_BASE_DELAY_MS = 1000;
const WS_RECONNECT_DEFAULT_MAX_DELAY_MS = 15000;
const RUNTIME_TIMELINE_MAX_ITEMS = 400;

const buildInitialRuntimeTimeline = (): RuntimeTimelineItem[] => {
  const now = new Date().toLocaleTimeString("ko-KR", { hour12: false });
  return [
    {
      id: "runtime-init",
      timeLabel: now,
      category: "event",
      message: "System initialized. Awaiting a new request.",
    },
  ];
};

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
  approval_stage: null,
  approval_requested_at: null,
  approval_pending_next_roles: [],
  approval_last_action: null,
  approval_last_stage: null,
  approval_last_actor: null,
  approval_last_note: null,
  approval_last_decision_at: null,
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

const isNetworkRole = (value: unknown): value is NetworkRole =>
  typeof value === "string" && NETWORK_ROLE_SET.has(value);

const clampInt = (value: number, min: number, max: number): number => {
  if (!Number.isFinite(value)) {
    return min;
  }
  return Math.max(min, Math.min(max, Math.trunc(value)));
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
  const [reconnectMaxAttempts, setReconnectMaxAttempts] = useState(WS_RECONNECT_DEFAULT_MAX_ATTEMPTS);
  const [reconnectBaseDelaySec, setReconnectBaseDelaySec] = useState(WS_RECONNECT_DEFAULT_BASE_DELAY_MS / 1000);
  const [reconnectMaxDelaySec, setReconnectMaxDelaySec] = useState(WS_RECONNECT_DEFAULT_MAX_DELAY_MS / 1000);
  const [modelOptions, setModelOptions] = useState<string[]>(FALLBACK_MODELS);
  const [fallbackModelOptions, setFallbackModelOptions] = useState<string[]>([]);
  const [requestedModelForRun, setRequestedModelForRun] = useState<string | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [isReconnecting, setIsReconnecting] = useState(false);
  const [showOperatorPanel, setShowOperatorPanel] = useState(false);
  const [runtimeSearch, setRuntimeSearch] = useState("");
  const [runtimeRoleFilter, setRuntimeRoleFilter] = useState<RuntimeRoleFilter>("all");
  const [autoScrollTimeline, setAutoScrollTimeline] = useState(true);
  const [fileFilter, setFileFilter] = useState("");
  const [copiedFilePath, setCopiedFilePath] = useState<string | null>(null);
  const [currentClientId, setCurrentClientId] = useState<string | null>(null);
  const [activeAgent, setActiveAgent] = useState<string>("idle");
  const [runtimeTimeline, setRuntimeTimeline] = useState<RuntimeTimelineItem[]>(() => buildInitialRuntimeTimeline());
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
  const [requeueCandidate, setRequeueCandidate] = useState<RequeueCandidate | null>(null);
  const [approvalRejectRole, setApprovalRejectRole] = useState<NetworkRole>("pm");
  const [approvalNote, setApprovalNote] = useState("");
  const [approvalBusyAction, setApprovalBusyAction] = useState<"approve" | "reject" | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const lastNodeRef = useRef<string | null>(null);
  const lastModelRef = useRef<string | null>(null);
  const runtimeSeqRef = useRef(0);
  const agentChatSeenRef = useRef<Set<string>>(new Set());
  const timelineScrollRef = useRef<HTMLDivElement | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef(0);
  const activeRunIdRef = useRef<string | null>(null);
  const runTerminalRef = useRef(false);
  const reconnectPolicyRef = useRef({
    maxAttempts: WS_RECONNECT_DEFAULT_MAX_ATTEMPTS,
    baseDelayMs: WS_RECONNECT_DEFAULT_BASE_DELAY_MS,
    maxDelayMs: WS_RECONNECT_DEFAULT_MAX_DELAY_MS,
  });

  const nextRuntimeId = (prefix: string) => {
    runtimeSeqRef.current += 1;
    return `${prefix}-${runtimeSeqRef.current}`;
  };

  const addEvent = (message: string) => {
    const ts = new Date().toLocaleTimeString("ko-KR", { hour12: false });
    setRuntimeTimeline((prev) => [
      ...prev.slice(-(RUNTIME_TIMELINE_MAX_ITEMS - 1)),
      {
        id: nextRuntimeId("event"),
        timeLabel: ts,
        category: "event",
        message,
      },
    ]);
  };

  const clearReconnectTimer = () => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  };

  const closeCurrentSocket = () => {
    clearReconnectTimer();

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
    if (role === "pm") return "border-amber-400/45 bg-amber-500/18 text-amber-100";
    if (role === "architect") return "border-sky-400/45 bg-sky-500/18 text-sky-100";
    if (role === "developer" || role === "developer_backend" || role === "developer_frontend") {
      return "border-teal-400/45 bg-teal-500/18 text-teal-100";
    }
    if (role === "tool_execution") return "border-cyan-400/45 bg-cyan-500/18 text-cyan-100";
    if (role === "qa") return "border-rose-400/45 bg-rose-500/18 text-rose-100";
    if (role === "github_deploy") return "border-indigo-400/45 bg-indigo-500/18 text-indigo-100";
    return "border-[color:var(--line)] bg-[color:var(--surface-soft)] text-[color:var(--text)]";
  };

  const syncRunSnapshot = async (runId: string): Promise<string | null> => {
    try {
      const response = await fetch(`${buildApiBaseUrl()}/api/network/runs/${runId}`);
      if (!response.ok) {
        return null;
      }

      const payload = (await response.json()) as {
        status?: string;
        active_node?: string | null;
        selected_model?: string | null;
        total_tokens?: number;
        total_cost?: number;
        approval_stage?: string | null;
        approval_requested_at?: string | null;
        approval_pending_next_roles?: string[];
        approval_last_action?: string | null;
        approval_last_stage?: string | null;
        approval_last_actor?: string | null;
        approval_last_note?: string | null;
        approval_last_decision_at?: string | null;
      };

      if (activeRunIdRef.current !== runId) {
        return null;
      }

      const status = typeof payload.status === "string" ? payload.status : "unknown";
      const activeNode = typeof payload.active_node === "string" ? payload.active_node : null;
      const selectedModelName = typeof payload.selected_model === "string" ? payload.selected_model : null;
      const totalTokens = typeof payload.total_tokens === "number" ? payload.total_tokens : undefined;
      const totalCost = typeof payload.total_cost === "number" ? payload.total_cost : undefined;
      const approvalStage = typeof payload.approval_stage === "string" ? payload.approval_stage : null;
      const approvalRequestedAt = typeof payload.approval_requested_at === "string" ? payload.approval_requested_at : null;
      const approvalPendingNextRoles = Array.isArray(payload.approval_pending_next_roles)
        ? payload.approval_pending_next_roles.filter((item): item is string => typeof item === "string")
        : undefined;
      const approvalLastAction = typeof payload.approval_last_action === "string" ? payload.approval_last_action : null;
      const approvalLastStage = typeof payload.approval_last_stage === "string" ? payload.approval_last_stage : null;
      const approvalLastActor = typeof payload.approval_last_actor === "string" ? payload.approval_last_actor : null;
      const approvalLastNote = typeof payload.approval_last_note === "string" ? payload.approval_last_note : null;
      const approvalLastDecisionAt = typeof payload.approval_last_decision_at === "string"
        ? payload.approval_last_decision_at
        : null;

      setWorkflowState((prev) => ({
        ...prev,
        status,
        active_node: activeNode ?? prev.active_node,
        selected_model: selectedModelName ?? prev.selected_model,
        total_tokens: totalTokens ?? prev.total_tokens,
        total_cost: totalCost ?? prev.total_cost,
        approval_stage: approvalStage,
        approval_requested_at: approvalRequestedAt,
        approval_pending_next_roles: approvalPendingNextRoles ?? prev.approval_pending_next_roles,
        approval_last_action: approvalLastAction,
        approval_last_stage: approvalLastStage,
        approval_last_actor: approvalLastActor,
        approval_last_note: approvalLastNote,
        approval_last_decision_at: approvalLastDecisionAt,
      }));

      if (activeNode) {
        lastNodeRef.current = activeNode;
        setActiveAgent(mapActiveAgent(activeNode));
      }
      if (selectedModelName) {
        lastModelRef.current = selectedModelName;
      }

      setLastUpdateAt(new Date().toLocaleTimeString("ko-KR", { hour12: false }));

      if (status === "completed") {
        runTerminalRef.current = true;
        setIsRunning(false);
        setIsReconnecting(false);
        setActiveAgent("done");
      } else if (status === "failed") {
        runTerminalRef.current = true;
        setIsRunning(false);
        setIsReconnecting(false);
      } else if (status === "waiting_approval") {
        setIsRunning(true);
        setIsReconnecting(false);
      } else {
        setIsRunning(true);
      }

      return status;
    } catch {
      return null;
    }
  };

  useEffect(() => {
    const el = timelineScrollRef.current;
    if (!el) return;
    if (!autoScrollTimeline) return;
    el.scrollTop = el.scrollHeight;
  }, [autoScrollTimeline, runtimeTimeline]);

  useEffect(() => {
    const files = workflowState.source_code ? Object.keys(workflowState.source_code).sort() : [];
    const normalizedFilter = fileFilter.trim().toLowerCase();
    const visibleFiles = normalizedFilter
      ? files.filter((path) => path.toLowerCase().includes(normalizedFilter))
      : files;

    if (files.length === 0) {
      setSelectedFile(null);
      return;
    }

    if (visibleFiles.length === 0) {
      return;
    }

    if (!selectedFile || !workflowState.source_code?.[selectedFile] || !visibleFiles.includes(selectedFile)) {
      setSelectedFile(visibleFiles[0]);
    }
  }, [fileFilter, workflowState.source_code, selectedFile]);

  useEffect(() => {
    if (!copiedFilePath) return;
    const timer = window.setTimeout(() => setCopiedFilePath(null), 1200);
    return () => window.clearTimeout(timer);
  }, [copiedFilePath]);

  useEffect(() => {
    if (!isNetworkRole(workflowState.approval_stage)) return;
    setApprovalRejectRole(workflowState.approval_stage);
  }, [workflowState.approval_stage]);

  useEffect(() => {
    const maxAttempts = clampInt(reconnectMaxAttempts, 1, 20);
    const baseDelayMs = clampInt(reconnectBaseDelaySec * 1000, 500, 60000);
    const maxDelayMsRaw = clampInt(reconnectMaxDelaySec * 1000, 1000, 120000);
    const maxDelayMs = Math.max(baseDelayMs, maxDelayMsRaw);

    reconnectPolicyRef.current = {
      maxAttempts,
      baseDelayMs,
      maxDelayMs,
    };
  }, [reconnectBaseDelaySec, reconnectMaxAttempts, reconnectMaxDelaySec]);

  useEffect(() => {
    return () => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }

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
    setRequeueCandidate(null);
    if (showOperatorPanel) {
      void refreshAdminPanel(true);
    }
  }, [adminRole, refreshAdminPanel, showOperatorPanel]);

  useEffect(() => {
    if (!showOperatorPanel) {
      return;
    }
    const intervalMs = isRunning ? 5000 : 15000;
    const timer = window.setInterval(() => {
      void refreshAdminPanel(false);
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [isRunning, refreshAdminPanel, showOperatorPanel]);

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

  const handleApprovalDecision = async (action: "approve" | "reject") => {
    if (!currentClientId) return;
    if (workflowState.status !== "waiting_approval") {
      addEvent("[APPROVAL] 현재 승인 대기 상태가 아닙니다.");
      return;
    }

    setApprovalBusyAction(action);
    try {
      const response = await fetch(`${buildApiBaseUrl()}/api/network/runs/${currentClientId}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action,
          actor: "director-ui",
          note: approvalNote.trim() || undefined,
          reject_to_role: action === "reject" ? approvalRejectRole : undefined,
        }),
      });
      if (!response.ok) {
        throw new Error(await extractErrorMessage(response));
      }

      const payload = (await response.json()) as ApprovalDecisionResponse;
      const nextRole = payload.next_role ?? "-";
      const nextRoles = Array.isArray(payload.next_roles) ? payload.next_roles.join(", ") : "";

      if (action === "approve") {
        addEvent(`[APPROVAL] 승인 완료. next=${nextRoles || nextRole}`);
      } else {
        addEvent(`[APPROVAL] 반려 완료. reroute=${nextRole}`);
      }

      if (payload.status) {
        setWorkflowState((prev) => ({ ...prev, status: payload.status ?? prev.status }));
      }
      setApprovalNote("");
      await syncRunSnapshot(currentClientId);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Approval decision failed";
      setWorkflowState((prev) => ({ ...prev, error: message }));
      addEvent(`[ERROR] ${message}`);
    } finally {
      setApprovalBusyAction(null);
    }
  };

  const handleWorkflowMessage = (raw: WorkflowMessage) => {
    if (raw.error) {
      runTerminalRef.current = true;
      setWorkflowState((prev) => ({ ...prev, error: raw.error ?? "Unknown workflow error" }));
      addEvent(`[ERROR] ${raw.error}`);
      setIsRunning(false);
      setIsReconnecting(false);
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
    if (raw.approval_stage !== undefined) patch.approval_stage = raw.approval_stage ?? null;
    if (raw.approval_requested_at !== undefined) patch.approval_requested_at = raw.approval_requested_at ?? null;
    if (raw.approval_pending_next_roles !== undefined) {
      patch.approval_pending_next_roles = Array.isArray(raw.approval_pending_next_roles)
        ? raw.approval_pending_next_roles.filter((item): item is string => typeof item === "string")
        : [];
    }
    if (raw.approval_last_action !== undefined) patch.approval_last_action = raw.approval_last_action ?? null;
    if (raw.approval_last_stage !== undefined) patch.approval_last_stage = raw.approval_last_stage ?? null;
    if (raw.approval_last_actor !== undefined) patch.approval_last_actor = raw.approval_last_actor ?? null;
    if (raw.approval_last_note !== undefined) patch.approval_last_note = raw.approval_last_note ?? null;
    if (raw.approval_last_decision_at !== undefined) {
      patch.approval_last_decision_at = raw.approval_last_decision_at ?? null;
    }

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
      const timeLabel = toTimeLabel(raw.updated_at);
      const dedupeKey = `${raw.updated_at ?? ""}|${fromRole}|${toRole}|${messageText}`;
      if (!agentChatSeenRef.current.has(dedupeKey)) {
        agentChatSeenRef.current.add(dedupeKey);
        setRuntimeTimeline((prev) => [
          ...prev.slice(-(RUNTIME_TIMELINE_MAX_ITEMS - 1)),
          {
            id: nextRuntimeId("chat"),
            timeLabel,
            category: "chat",
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
      runTerminalRef.current = true;
      setActiveAgent("done");
      setIsRunning(false);
      setIsReconnecting(false);
      addEvent("Workflow completed.");
      return;
    }

    if (raw.status === "failed") {
      runTerminalRef.current = true;
      setIsRunning(false);
      setIsReconnecting(false);
      setWorkflowState((prev) => ({ ...prev, error: raw.message ?? prev.error ?? "Workflow failed" }));
      addEvent(`Workflow failed.${raw.message ? ` ${raw.message}` : ""}`);
      return;
    }

    if (raw.status === "waiting_approval") {
      setIsReconnecting(false);
      setIsRunning(true);
    }

    if (raw.status && raw.status !== "completed") {
      addEvent(`Status: ${raw.status}`);
    }
  };

  const connectNetworkWorkflow = (runId: string, options?: { isRetry?: boolean }) => {
    clearReconnectTimer();
    closeCurrentSocket();
    const socket = new WebSocket(`${buildWsBaseUrl()}/ws/network/${runId}`);
    wsRef.current = socket;
    setIsReconnecting(Boolean(options?.isRetry));

    socket.onopen = () => {
      setIsConnected(true);
      setIsReconnecting(false);
      reconnectAttemptRef.current = 0;
      if (options?.isRetry) {
        addEvent("[WS] Reconnected.");
      }
      void syncRunSnapshot(runId);
    };

    socket.onclose = () => {
      setIsConnected(false);

      if (activeRunIdRef.current !== runId) {
        return;
      }
      if (runTerminalRef.current) {
        setIsRunning(false);
        setIsReconnecting(false);
        return;
      }

      void (async () => {
        const syncedStatus = await syncRunSnapshot(runId);
        if (syncedStatus === "completed" || syncedStatus === "failed") {
          return;
        }

        const policy = reconnectPolicyRef.current;

        const nextAttempt = reconnectAttemptRef.current + 1;
        reconnectAttemptRef.current = nextAttempt;

        if (nextAttempt > policy.maxAttempts) {
          setIsReconnecting(false);
          setIsRunning(false);
          setWorkflowState((prev) => ({
            ...prev,
            error: "WebSocket 재연결이 반복 실패했습니다. 상태를 확인한 뒤 다시 실행해주세요.",
          }));
          addEvent("[ERROR] WebSocket reconnect failed.");
          return;
        }

        const delayMs = Math.min(policy.maxDelayMs, policy.baseDelayMs * (2 ** (nextAttempt - 1)));
        setIsReconnecting(true);
        addEvent(
          `[WS] Disconnected. Reconnecting in ${Math.round(delayMs / 1000)}s (${nextAttempt}/${policy.maxAttempts})`,
        );

        reconnectTimerRef.current = window.setTimeout(() => {
          if (activeRunIdRef.current !== runId || runTerminalRef.current) {
            return;
          }
          connectNetworkWorkflow(runId, { isRetry: true });
        }, delayMs);
      })();
    };

    socket.onerror = () => {
      setIsConnected(false);
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

    closeCurrentSocket();
    activeRunIdRef.current = null;
    runTerminalRef.current = false;
    reconnectAttemptRef.current = 0;
    lastNodeRef.current = null;
    setWorkflowState({ ...initialWorkflowState, selected_model: selectedModel });
    setRequestedModelForRun(selectedModel);
    lastModelRef.current = selectedModel;
    agentChatSeenRef.current.clear();
    setRuntimeTimeline([
      {
        id: nextRuntimeId("event"),
        timeLabel: new Date().toLocaleTimeString("ko-KR", { hour12: false }),
        category: "event",
        message: `Run requested (model=${selectedModel})`,
      },
    ]);
    setCurrentClientId(null);
    setActiveAgent("pm");
    setApprovalNote("");
    setApprovalBusyAction(null);
    setApprovalRejectRole("pm");
    setIsConnected(false);
    setIsReconnecting(false);
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
      activeRunIdRef.current = runId;
      setCurrentClientId(runId);
      setWorkflowState((prev) => ({ ...prev, selected_model: resolvedModel }));
      lastModelRef.current = resolvedModel;
      addEvent(`Network session created: ${runId} (requested=${selectedModel}, active=${resolvedModel})`);
      connectNetworkWorkflow(runId);
    } catch (error) {
      activeRunIdRef.current = null;
      runTerminalRef.current = true;
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
  const normalizedFileFilter = fileFilter.trim().toLowerCase();
  const visibleFiles = normalizedFileFilter
    ? sortedFiles.filter((path) => path.toLowerCase().includes(normalizedFileFilter))
    : sortedFiles;
  const selectedCode = selectedFile && workflowState.source_code ? workflowState.source_code[selectedFile] : "";
  const normalizedRuntimeSearch = runtimeSearch.trim().toLowerCase();
  const filteredRuntimeTimeline = runtimeTimeline.filter((item) => {
    const matchesSearch = normalizedRuntimeSearch
      ? `${item.timeLabel} ${item.category} ${item.fromRole ?? ""} ${item.toRole ?? ""} ${item.message}`
        .toLowerCase()
        .includes(normalizedRuntimeSearch)
      : true;
    const matchesRole = runtimeRoleFilter === "all"
      ? true
      : runtimeRoleFilter === "event"
        ? item.category === "event"
        : item.fromRole === runtimeRoleFilter || item.toRole === runtimeRoleFilter;
    return matchesSearch && matchesRole;
  });
  const timelineChatCount = runtimeTimeline.filter((item) => item.category === "chat").length;
  const timelineEventCount = runtimeTimeline.length - timelineChatCount;
  const executionLogText = [
    workflowState.current_logs ? workflowState.current_logs : "No execution logs yet.",
    workflowState.qa_report ? workflowState.qa_report : "",
    workflowState.github_deploy_log ? workflowState.github_deploy_log : "",
    workflowState.error ? `[ERROR] ${workflowState.error}` : "",
  ].filter(Boolean).join("\n\n");
  const filteredExecutionLogLines = normalizedRuntimeSearch
    ? executionLogText.split("\n").filter((line) => line.toLowerCase().includes(normalizedRuntimeSearch))
    : executionLogText.split("\n");
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
  const approvalRequestedLabel = workflowState.approval_requested_at
    ? toTimeLabel(workflowState.approval_requested_at)
    : "-";
  const approvalLastDecisionLabel = workflowState.approval_last_decision_at
    ? toTimeLabel(workflowState.approval_last_decision_at)
    : "-";
  const approvalPendingRolesLabel = workflowState.approval_pending_next_roles.length > 0
    ? workflowState.approval_pending_next_roles.join(", ")
    : "-";

  const waitingApproval = workflowState.status === "waiting_approval";
  const statusLabel = waitingApproval
    ? "승인 대기"
    : !isRunning
      ? "대기"
      : isConnected
        ? "실행 중"
        : isReconnecting
          ? "재연결 중"
          : "연결 중";
  const statusClass = waitingApproval ? "tag-amber" : !isRunning ? "tag-rose" : isConnected ? "tag-teal" : "tag-amber";
  const statusDotClass = waitingApproval ? "status-wait" : !isRunning ? "status-idle" : isConnected ? "status-live" : "status-wait";
  const handleCopySelectedCode = async () => {
    if (!selectedCode || !selectedFile) return;
    try {
      await navigator.clipboard.writeText(selectedCode);
      setCopiedFilePath(selectedFile);
    } catch {
      addEvent("[ERROR] Clipboard copy failed.");
    }
  };

  return (
    <div className="app-shell">
      <div className="app-container space-y-5 md:space-y-6">
        <section className="panel panel-elevated p-5 md:p-7 reveal" style={{ animationDelay: "50ms" }}>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="max-w-4xl">
              <p className="panel-title">워크플로우 대시보드</p>
              <h1 className="mt-2 text-2xl md:text-4xl font-semibold tracking-tight leading-tight">
                AI Orchestrator
              </h1>
              <p className="mt-3 text-sm md:text-[15px] text-[color:var(--text-muted)]">
                요청을 실행하고 팀 에이전트 대화, 생성 코드, 운영 큐 상태를 확인하는 화면입니다.
                실행 제어와 장애 복구(DLQ Redrive)를 한곳에서 처리할 수 있습니다.
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
              <span className="tag tag-amber">Network Run</span>
            </div>
          </div>

          <div className="mt-5 grid gap-2.5 lg:grid-cols-[1fr_180px_140px]">
            <textarea
              rows={2}
              placeholder="예: 사용자 요구사항을 입력하면 PM→Architect→Developer→QA→Deploy 순으로 자동 개발 플로우를 실행합니다."
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              disabled={isRunning}
              className="panel bg-[color:var(--surface-soft)] border-[color:var(--line)] px-3 py-2 text-[13px] resize-none outline-none focus:ring-2 focus:ring-teal-600/30 disabled:opacity-70"
            />

            <select
              value={selectedModel}
              onChange={(event) => setSelectedModel(event.target.value)}
              disabled={isRunning}
              className="panel bg-[color:var(--surface-soft)] border-[color:var(--line)] px-2.5 py-2 text-[13px] outline-none focus:ring-2 focus:ring-teal-600/30 disabled:opacity-70"
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
              className="rounded-xl border border-teal-700/40 bg-teal-700 text-white px-3.5 py-2 text-[13px] font-semibold inline-flex items-center justify-center gap-2 disabled:opacity-55 transition-colors hover:bg-teal-600"
            >
              <Play className="w-4 h-4" />
              {isRunning ? "실행 중..." : "실행"}
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

          {waitingApproval && (
            <div className="mt-3 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs font-semibold text-amber-100">디렉터 승인 대기</p>
                <span className="tag tag-amber mono">requested {approvalRequestedLabel}</span>
              </div>

              <div className="mt-2 grid gap-2 sm:grid-cols-2 text-[11px] text-amber-100/90">
                <p>stage: <span className="mono">{workflowState.approval_stage ?? "-"}</span></p>
                <p>next: <span className="mono">{approvalPendingRolesLabel}</span></p>
                <p>last_action: <span className="mono">{workflowState.approval_last_action ?? "-"}</span></p>
                <p>last_decision: <span className="mono">{approvalLastDecisionLabel}</span></p>
              </div>

              <div className="mt-3 grid gap-2 sm:grid-cols-[180px_1fr]">
                <label className="text-[11px] text-amber-100/90">
                  반려 대상 role
                  <select
                    value={approvalRejectRole}
                    onChange={(event) => setApprovalRejectRole(event.target.value as NetworkRole)}
                    disabled={approvalBusyAction !== null}
                    className="mt-1 w-full rounded-md border border-amber-400/35 bg-[#1b2737] px-2 py-1.5 text-xs mono outline-none focus:ring-2 focus:ring-amber-500/30 disabled:opacity-60"
                  >
                    {NETWORK_ROLES.map((roleName) => (
                      <option key={roleName} value={roleName}>
                        {roleName}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-[11px] text-amber-100/90">
                  코멘트
                  <input
                    type="text"
                    value={approvalNote}
                    onChange={(event) => setApprovalNote(event.target.value)}
                    disabled={approvalBusyAction !== null}
                    placeholder="예: API 스펙 보강 후 재검토"
                    className="mt-1 w-full rounded-md border border-amber-400/35 bg-[#1b2737] px-2 py-1.5 text-xs outline-none focus:ring-2 focus:ring-amber-500/30 disabled:opacity-60"
                  />
                </label>
              </div>

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => { void handleApprovalDecision("approve"); }}
                  disabled={!currentClientId || approvalBusyAction !== null}
                  className="rounded-lg border border-teal-700/35 bg-teal-700 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-60 hover:bg-teal-600"
                >
                  {approvalBusyAction === "approve" ? "승인 처리 중..." : "승인"}
                </button>
                <button
                  type="button"
                  onClick={() => { void handleApprovalDecision("reject"); }}
                  disabled={!currentClientId || approvalBusyAction !== null}
                  className="rounded-lg border border-rose-500/35 bg-rose-700 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-60 hover:bg-rose-600"
                >
                  {approvalBusyAction === "reject" ? "반려 처리 중..." : "반려"}
                </button>
              </div>
            </div>
          )}

          <div className="mt-3 grid gap-2 sm:grid-cols-3 lg:max-w-3xl">
            <label className="text-[11px] text-[color:var(--text-muted)]">
              WS 재연결 횟수
              <input
                type="number"
                min={1}
                max={20}
                step={1}
                value={reconnectMaxAttempts}
                onChange={(event) => {
                  const parsed = Number(event.target.value);
                  if (!Number.isFinite(parsed)) return;
                  setReconnectMaxAttempts(parsed);
                }}
                className="mt-1 w-full rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-2 py-2 text-xs mono outline-none focus:ring-2 focus:ring-teal-600/30"
              />
            </label>
            <label className="text-[11px] text-[color:var(--text-muted)]">
              기본 지연(초)
              <input
                type="number"
                min={0.5}
                max={60}
                step={0.5}
                value={reconnectBaseDelaySec}
                onChange={(event) => {
                  const parsed = Number(event.target.value);
                  if (!Number.isFinite(parsed)) return;
                  setReconnectBaseDelaySec(parsed);
                }}
                className="mt-1 w-full rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-2 py-2 text-xs mono outline-none focus:ring-2 focus:ring-teal-600/30"
              />
            </label>
            <label className="text-[11px] text-[color:var(--text-muted)]">
              최대 지연(초)
              <input
                type="number"
                min={1}
                max={120}
                step={1}
                value={reconnectMaxDelaySec}
                onChange={(event) => {
                  const parsed = Number(event.target.value);
                  if (!Number.isFinite(parsed)) return;
                  setReconnectMaxDelaySec(parsed);
                }}
                className="mt-1 w-full rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-2 py-2 text-xs mono outline-none focus:ring-2 focus:ring-teal-600/30"
              />
            </label>
          </div>
          <p className="mt-1 text-[11px] text-[color:var(--text-muted)]">
            연결 끊김 시 지수 백오프로 재시도합니다. 현재 설정은 즉시 적용됩니다.
          </p>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setShowOperatorPanel((prev) => !prev)}
              className={`rounded-md border px-3 py-1.5 text-xs font-semibold transition-colors ${
                showOperatorPanel
                  ? "border-teal-700/35 bg-teal-700 text-white hover:bg-teal-600"
                  : "border-[color:var(--line)] bg-[color:var(--surface-soft)] text-[color:var(--text-muted)] hover:bg-[#1b2737]"
              }`}
            >
              {showOperatorPanel ? "운영 패널 숨기기" : "운영 패널 보기"}
            </button>
            <span className="text-[11px] text-[color:var(--text-muted)]">
              큐 상태와 DLQ Redrive가 필요할 때만 운영 패널을 펼쳐서 확인합니다.
            </span>
          </div>
        </section>

        <section className="grid grid-cols-2 xl:grid-cols-6 gap-3 reveal" style={{ animationDelay: "100ms" }}>
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
          <div className="panel p-4">
            <p className="panel-title">Queue / DLQ</p>
            <p className="mt-2 text-sm font-semibold">{pendingTotal} / {dlqTotal}</p>
          </div>
          <div className="panel p-4">
            <p className="panel-title">Consumers</p>
            <p className="mt-2 text-lg font-semibold">{consumerTotal}</p>
          </div>
        </section>

        <section className="panel p-4 md:p-5 reveal" style={{ animationDelay: "150ms" }}>
          <div className="flex items-center justify-between gap-4">
            <p className="panel-title">진행 상태</p>
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
                    ? "border-teal-400/45 bg-teal-500/16"
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
              <p className="panel-title">실행 로그</p>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <input
                value={runtimeSearch}
                onChange={(event) => setRuntimeSearch(event.target.value)}
                placeholder="Runtime 검색 (chat/event/log)"
                className="w-full sm:w-64 rounded-md border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-2 py-1.5 text-xs outline-none focus:ring-2 focus:ring-teal-600/30"
              />
              <label className="inline-flex items-center gap-1 rounded-md border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-2 py-1 text-[10px] mono">
                role
                <select
                  value={runtimeRoleFilter}
                  onChange={(event) => setRuntimeRoleFilter(event.target.value as RuntimeRoleFilter)}
                  className="rounded border border-[color:var(--line)] bg-[color:var(--surface)] px-1 py-0.5 text-[10px] outline-none"
                >
                  <option value="all">all</option>
                  <option value="event">event-only</option>
                  {NETWORK_ROLES.map((roleName) => (
                    <option key={roleName} value={roleName}>
                      {roleName}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inline-flex items-center gap-1 rounded-md border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-2 py-1 text-[10px] mono">
                <input
                  type="checkbox"
                  checked={autoScrollTimeline}
                  onChange={(event) => setAutoScrollTimeline(event.target.checked)}
                />
                auto-scroll
              </label>
              <button
                type="button"
                onClick={() => {
                  agentChatSeenRef.current.clear();
                  setRuntimeTimeline([
                    {
                      id: nextRuntimeId("event"),
                      timeLabel: new Date().toLocaleTimeString("ko-KR", { hour12: false }),
                      category: "event",
                      message: "Runtime feed cleared.",
                    },
                  ]);
                }}
                className="rounded-md border border-[color:var(--line)] bg-[color:var(--surface)] px-2 py-1 text-[10px] mono hover:bg-[#1b2737]"
              >
                clear
              </button>
              <span className="text-[11px] text-[color:var(--text-muted)] mono">
                chat {timelineChatCount} | event {timelineEventCount} | filter {runtimeRoleFilter}
              </span>
            </div>
            <div className="mt-3 rounded-xl border border-[color:var(--line)] bg-[color:var(--surface-soft)] p-3 h-[292px] overflow-y-auto custom-scrollbar" ref={timelineScrollRef}>
              <div className="flex items-center gap-1 mb-2">
                <MessageCircle className="w-3 h-3" />
                <p className="text-[11px] text-[color:var(--text-muted)] font-semibold">
                  대화/이벤트 타임라인 ({filteredRuntimeTimeline.length}/{runtimeTimeline.length})
                </p>
              </div>
              {filteredRuntimeTimeline.length === 0 ? (
                <p className="text-[11px] mono text-[color:var(--text-muted)]">
                  {runtimeTimeline.length === 0 ? "No runtime events yet." : "검색 조건과 일치하는 항목이 없습니다."}
                </p>
              ) : (
                <div className="space-y-2">
                  {filteredRuntimeTimeline.map((item) => (
                    <div
                      key={item.id}
                      className={`rounded-lg border p-2 ${
                        item.category === "chat"
                          ? getRoleTone(item.fromRole ?? "")
                          : "border-[color:var(--line)] bg-[color:var(--surface-soft)] text-[color:var(--text)]"
                      }`}
                    >
                      <p className="mono text-[10px] opacity-75">
                        {item.category === "chat"
                          ? `${item.timeLabel} ${item.fromRole ?? "agent"} -> ${item.toRole ?? "supervisor"}`
                          : `${item.timeLabel} event`}
                      </p>
                      <p className="mt-1 text-[11px] whitespace-pre-wrap leading-relaxed">{item.message}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className="mt-3 rounded-xl border border-[color:var(--line)] bg-[color:var(--surface-soft)] p-3 h-[130px] overflow-y-auto custom-scrollbar">
              <p className="text-[11px] text-[color:var(--text-muted)] font-semibold mb-2">Execution / QA / Deploy</p>
              <pre className="whitespace-pre-wrap text-[11px] mono">
                {filteredExecutionLogLines.length
                  ? filteredExecutionLogLines.join("\n")
                  : "검색 조건과 일치하는 실행 로그가 없습니다."}
              </pre>
            </div>
          </div>
        </section>

        <section className="panel p-4 md:p-5 min-h-[460px] reveal" style={{ animationDelay: "240ms" }}>
          <div className="flex items-center gap-2 pb-3 border-b border-[color:var(--line)]">
            <FileCode2 className="w-4 h-4" />
            <p className="panel-title">Source Explorer</p>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <input
              value={fileFilter}
              onChange={(event) => setFileFilter(event.target.value)}
              placeholder="파일 경로 검색"
              className="w-full sm:w-60 rounded-md border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-2 py-1.5 text-xs mono outline-none focus:ring-2 focus:ring-teal-600/30"
            />
            <button
              type="button"
              onClick={() => { void handleCopySelectedCode(); }}
              disabled={!selectedCode}
              className="rounded-md border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-2.5 py-1.5 text-xs font-semibold disabled:opacity-55 hover:bg-[#1b2737]"
            >
              {copiedFilePath && copiedFilePath === selectedFile ? "Copied" : "Copy selected"}
            </button>
            <span className="text-[11px] text-[color:var(--text-muted)]">
              {visibleFiles.length}/{sortedFiles.length} files
            </span>
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-[180px_1fr] h-[380px]">
            <div className="rounded-xl border border-[color:var(--line)] bg-[color:var(--surface-soft)] overflow-y-auto custom-scrollbar">
              {sortedFiles.length === 0 ? (
                <div className="p-3 text-xs text-[color:var(--text-muted)]">No generated files.</div>
              ) : visibleFiles.length === 0 ? (
                <div className="p-3 text-xs text-[color:var(--text-muted)]">검색 조건과 일치하는 파일이 없습니다.</div>
              ) : (
                visibleFiles.map((path) => (
                  <button
                    key={path}
                    type="button"
                    onClick={() => setSelectedFile(path)}
                    className={`w-full text-left px-3 py-2 text-xs mono border-b border-[color:var(--line)] transition-colors ${
                      selectedFile === path
                        ? "bg-teal-500/20 text-teal-100"
                        : "text-[color:var(--text)] hover:bg-[#1b2737]"
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

        {showOperatorPanel && (
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
                className="inline-flex items-center gap-2 rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-3 py-2 text-xs font-semibold disabled:opacity-60 hover:bg-[#1b2737]"
              >
                <RefreshCw className={`w-3 h-3 ${isQueueLoading || isDlqLoading ? "animate-spin" : ""}`} />
                Refresh
              </button>
            </div>

            {adminError && (
              <div className="mt-3 rounded-lg border border-rose-400/45 bg-rose-500/16 text-rose-100 p-2 text-xs">
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
                    <p className="mt-2 text-xs text-rose-200 whitespace-pre-wrap break-words">{summary.error}</p>
                    <button
                      type="button"
                      onClick={() => {
                        setRequeueCandidate({
                          entryId: entry.entry_id,
                          runId: summary.runId,
                          attemptLabel: `${summary.attempt}/${summary.maxAttempts}`,
                        });
                      }}
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

        {requeueCandidate && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4">
            <div className="w-full max-w-md rounded-lg border border-[color:var(--line)] bg-[color:var(--surface)] p-4 shadow-xl">
              <p className="panel-title">Confirm DLQ Requeue</p>
              <p className="mt-3 text-sm text-[color:var(--text)]">
                아래 항목을 다시 큐에 넣을까요?
              </p>
              <div className="mt-3 space-y-1 text-xs text-[color:var(--text-muted)] mono">
                <p>entry {requeueCandidate.entryId}</p>
                <p>run {requeueCandidate.runId}</p>
                <p>attempt {requeueCandidate.attemptLabel}</p>
                <p>source {adminRole}</p>
                <p>target {redriveTargetRole}</p>
              </div>
              <div className="mt-4 flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setRequeueCandidate(null)}
                  className="rounded-lg border border-[color:var(--line)] bg-[color:var(--surface-soft)] px-3 py-2 text-xs font-semibold hover:bg-[#1b2737]"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => {
                    const entryId = requeueCandidate.entryId;
                    setRequeueCandidate(null);
                    void handleRequeueDlqEntry(entryId);
                  }}
                  disabled={redriveEntryId === requeueCandidate.entryId}
                  className="rounded-lg border border-teal-700/35 bg-teal-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-60 hover:bg-teal-600"
                >
                  {redriveEntryId === requeueCandidate.entryId ? "Requeueing..." : "Confirm Requeue"}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
