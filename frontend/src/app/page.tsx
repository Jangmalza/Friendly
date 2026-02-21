"use client";

import { useState, useEffect, useRef } from "react";
import { Terminal, Lightbulb, PenTool, LayoutTemplate, Play, Activity } from "lucide-react";
import { motion } from "framer-motion";

type AgentState = "idle" | "running" | "completed" | "error";

interface WorkflowState {
  pm_spec: string | null;
  architecture_spec: string | null;
  current_logs: string | null;
  status?: string;
  error?: string;
}

export default function Dashboard() {
  const [prompt, setPrompt] = useState("");
  const [isConnected, setIsConnected] = useState(false);
  const [activeAgent, setActiveAgent] = useState<string>("pm");

  const [workflowState, setWorkflowState] = useState<WorkflowState>({
    pm_spec: null,
    architecture_spec: null,
    current_logs: null,
  });

  const ws = useRef<WebSocket | null>(null);

  useEffect(() => {
    // Need to configure the correct WebSocket URL based on proxy or local setup
    ws.current = new WebSocket("ws://127.0.0.1:8000/ws/orchestrate");

    ws.current.onopen = () => setIsConnected(true);
    ws.current.onclose = () => setIsConnected(false);

    ws.current.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        if (data.error) {
          console.error("Workflow error:", data.error);
          return;
        }

        if (data.status === "completed") {
          setActiveAgent("done");
          return;
        }

        setWorkflowState((prev) => ({
          ...prev,
          ...data
        }));

        // Determine active agent dynamically based on populated specs (Simplified logic)
        if (data.pm_spec && !data.architecture_spec) setActiveAgent("architect");
        else if (data.architecture_spec && !data.current_logs) setActiveAgent("developer");
        else if (data.current_logs) setActiveAgent("qa");

      } catch (e) {
        console.error("Error parsing WS message", e);
      }
    };

    return () => {
      ws.current?.close();
    };
  }, []);

  const handleStartWorkflow = () => {
    if (!prompt.trim() || !ws.current) return;

    // Reset state
    setWorkflowState({
      pm_spec: null,
      architecture_spec: null,
      current_logs: null,
    });
    setActiveAgent("pm");

    ws.current.send(JSON.stringify({ prompt }));
  };


  const agents = [
    { id: "pm", name: "PM / 요구사항 분석", icon: Lightbulb },
    { id: "architect", name: "아키텍트 / 구조 설계", icon: LayoutTemplate },
    { id: "developer", name: "개발자 / 구현", icon: PenTool },
    { id: "qa", name: "QA / 테스트 및 검증", icon: Activity },
  ];

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-200 p-8 font-sans">
      <div className="max-w-6xl mx-auto space-y-8">

        {/* Header & Input */}
        <section className="space-y-4">
          <div className="flex items-center space-x-3">
            <Terminal className="text-emerald-400 w-8 h-8" />
            <h1 className="text-2xl font-semibold tracking-tight">AI 소프트웨어 개발 오케스트레이션</h1>
            <div className={`ml-auto px-3 py-1 rounded-full text-xs font-medium border ${isConnected ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : 'bg-red-500/10 text-red-400 border-red-500/20'}`}>
              {isConnected ? "WS Connected" : "Disconnected"}
            </div>
          </div>
          <div className="flex gap-4">
            <input
              type="text"
              placeholder="개발할 소프트웨어의 요구사항을 자연어로 입력하세요..."
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              className="flex-1 bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3 focus:outline-none focus:ring-2 focus:ring-emerald-500/50 transition-all text-neutral-100 placeholder-neutral-500"
              disabled={activeAgent !== "idle" && activeAgent !== "done" && activeAgent !== "pm"}
            />
            <button
              onClick={handleStartWorkflow}
              className="bg-emerald-600 hover:bg-emerald-500 text-white px-6 py-3 rounded-lg flex items-center gap-2 font-medium transition-colors"
            >
              <Play className="w-4 h-4" />
              Start Deployment
            </button>
          </div>
        </section>

        {/* Agent Status Bar */}
        <section className="grid grid-cols-4 gap-4">
          {agents.map((agent) => (
            <div
              key={agent.id}
              className={`
                  p-4 border rounded-xl flex items-center gap-4 transition-all duration-500
                  ${activeAgent === agent.id
                  ? 'border-emerald-500/50 bg-emerald-500/10 shadow-[0_0_15px_rgba(16,185,129,0.15)] glow'
                  : 'border-neutral-800 bg-neutral-900/50 opacity-50'}
                `}
            >
              <div className={`p-2 rounded-lg ${activeAgent === agent.id ? 'bg-emerald-500/20 text-emerald-400' : 'bg-neutral-800 text-neutral-500'}`}>
                <agent.icon className="w-5 h-5" />
              </div>
              <div>
                <h3 className="font-medium text-sm">{agent.name}</h3>
                <p className="text-xs text-neutral-500 mt-1">
                  {activeAgent === agent.id ? "작업 진행 중..." : "대기 중"}
                </p>
              </div>
            </div>
          ))}
        </section>

        {/* Live Editor / Monitoring Panels */}
        <section className="grid grid-cols-2 gap-6 h-[500px]">
          <div className="bg-neutral-900 border border-neutral-800 rounded-xl p-4 overflow-hidden flex flex-col">
            <div className="text-xs font-semibold uppercase tracking-wider text-neutral-500 mb-4 pb-2 border-b border-neutral-800">
              기획 명세서 및 아키텍처 (PM & Architect)
            </div>
            <div className="flex-1 overflow-y-auto whitespace-pre-wrap font-mono text-sm text-neutral-300 custom-scrollbar">
              {workflowState.pm_spec ? workflowState.pm_spec : "No PM Spec Generated."}
              {"\n\n"}
              {workflowState.architecture_spec ? workflowState.architecture_spec : ""}
            </div>
          </div>

          <div className="bg-neutral-900 border border-neutral-800 rounded-xl p-4 overflow-hidden flex flex-col">
            <div className="text-xs font-semibold uppercase tracking-wider text-neutral-500 mb-4 pb-2 border-b border-neutral-800">
              실행 로그 및 에러 피드백 (QA)
            </div>
            <div className="flex-1 overflow-y-auto whitespace-pre-wrap font-mono text-sm text-red-400 custom-scrollbar">
              {workflowState.current_logs ? workflowState.current_logs : "No execution logs yet."}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
