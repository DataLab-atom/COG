import { create } from 'zustand'
import type {
  Phase, StepStatus, WsEvent, Metrics,
  GatePayload, MctsNode, RunConfig, U2eHistoryPoint,
  MctsEvalProgress, U2eIterProgress, CodegenMapProgress,
  StepOutputDetail,
} from '@/types'
import {
  MCTS_COMBO_SUB_STEPS, type MctsComboSubStep,
  U2E_BODY_SUB_STEPS, type U2eBodySubStep,
  CODEGEN_MAP_SUB_STEPS, type CodegenMapSubStep,
  VERIFY_FIX_SUB_STEPS, type VerifyFixSubStep,
} from '@/types'

/** 步骤耗时记录 */
export interface StepTiming {
  startedAt: number
  finishedAt?: number
}

export interface RunState {
  runId:       string | null
  phase:       Phase
  elapsed:     number
  progress:    number

  /** WebSocket 连接状态 */
  wsConnected: boolean

  stepStatus:  Record<string, StepStatus>
  stepOutputs: Record<string, Record<string, unknown>>
  stepLogs:    Record<string, string[]>
  /** 每个步骤的开始/结束时间戳（毫秒） */
  stepTimings: Record<string, StepTiming>

  gateState:   GatePayload | null
  metrics:     Metrics

  mctsNodes:      Record<string, MctsNode>
  mctsFrontier:   string[]
  mctsGeneration: number

  u2eIteration:   number
  u2eFuncEvals:   number
  u2eMaxFe:       number
  u2eHistory:     U2eHistoryPoint[]

  /** 代码生成 fn_codegen map 子步骤进度（来自 arch_codegen_single 透传） */
  codegenMapProgress: CodegenMapProgress
  /** 沙盒验证修复子步骤状态（来自 arch_verify_and_fix 透传） */
  verifyFixStatus: Record<VerifyFixSubStep, StepStatus>
  /** 验证修复循环迭代次数 */
  verifyFixIter: number
  /** 架构验证 loop_to 重试次数（target step_id → count） */
  archRetries: Record<string, number>

  /** MCTS 当前代的 combo 评估子步骤进度（来自子图透传） */
  mctsEvalProgress: MctsEvalProgress
  /** MCTS gen_loop 迭代次数（来自 loop_jump 事件） */
  mctsLoopIter: number

  /** U2E 当前迭代的子步骤进度（来自子图透传） */
  u2eIterProgress: U2eIterProgress
  /** U2E 循环迭代次数（来自 loop_jump 事件） */
  u2eLoopIter: number

  /** LLM / 工具输出详情列表（按时间顺序，供步骤详情面板展示） */
  outputDetails: StepOutputDetail[]

  paperSteps:  Record<string, StepStatus>
  runError:    string | null

  startRun:       (config: RunConfig) => Promise<void>
  handleEvent:    (event: WsEvent) => void
  submitGate:     (gateId: string, payload: Record<string, unknown>) => Promise<void>
  abortRun:       () => Promise<void>
  reset:          () => void
  tickElapsed:    () => void
  setWsConnected: (connected: boolean) => void
}

const PAPER_STEP_IDS = [
  'build_method_explain', 'build_references', 'build_database',
  'experimenting', 'method_writing', 'related_work_writing',
  'experiment_writing', 'introduction_writing', 'merge_writing',
]

function inferPhase(stepStatus: Record<string, StepStatus>): Phase {
  // 辅助函数：判断步骤是否已启动（running 或 done 均表示已到达该阶段）
  const reached = (id: string) => stepStatus[id] === 'running' || stepStatus[id] === 'done'

  if (stepStatus['merge_writing'] === 'done') return 7
  if (PAPER_STEP_IDS.some(reached)) return 6
  if (reached('u2e_optimize') || reached('apply_u2e_best')) return 5
  if (reached('mcts_optimize') || reached('close_mcts_sandbox')) return 4
  if (reached('arch_to_project') || reached('gen_requirements')) return 3
  if (reached('arch_gate')) return 2
  if (reached('arch_step')) return 1
  return 0
}

function phaseProgress(phase: Phase): number {
  const weights: Record<Phase, number> = { 0:0, 1:10, 2:15, 3:22, 4:55, 5:75, 6:90, 7:100 }
  return weights[phase] ?? 0
}

const EMPTY_COMBO_COUNTS = Object.fromEntries(MCTS_COMBO_SUB_STEPS.map(s => [s, 0])) as Record<MctsComboSubStep, number>
const EMPTY_EVAL_PROGRESS: MctsEvalProgress = { started: { ...EMPTY_COMBO_COUNTS }, done: { ...EMPTY_COMBO_COUNTS }, beamCount: 0 }
const EMPTY_U2E_ITER: U2eIterProgress = Object.fromEntries(U2E_BODY_SUB_STEPS.map(s => [s, 'pending' as StepStatus])) as U2eIterProgress
const EMPTY_CODEGEN_COUNTS = Object.fromEntries(CODEGEN_MAP_SUB_STEPS.map(s => [s, 0])) as Record<CodegenMapSubStep, number>
const EMPTY_CODEGEN_PROGRESS: CodegenMapProgress = { started: { ...EMPTY_CODEGEN_COUNTS }, done: { ...EMPTY_CODEGEN_COUNTS }, totalFns: 0 }
const EMPTY_VERIFY_FIX = Object.fromEntries(VERIFY_FIX_SUB_STEPS.map(s => [s, 'pending' as StepStatus])) as Record<VerifyFixSubStep, StepStatus>

const INITIAL: Omit<RunState, 'startRun' | 'handleEvent' | 'submitGate' | 'abortRun' | 'reset' | 'tickElapsed' | 'setWsConnected'> = {
  runId: null, phase: 0, elapsed: 0, progress: 0,
  wsConnected: false,
  stepStatus: {}, stepOutputs: {}, stepLogs: {}, stepTimings: {},
  gateState: null, metrics: {},
  mctsNodes: {}, mctsFrontier: [], mctsGeneration: 0,
  codegenMapProgress: { ...EMPTY_CODEGEN_PROGRESS, started: { ...EMPTY_CODEGEN_COUNTS }, done: { ...EMPTY_CODEGEN_COUNTS } },
  verifyFixStatus: { ...EMPTY_VERIFY_FIX },
  verifyFixIter: 0,
  archRetries: {},
  mctsEvalProgress: { ...EMPTY_EVAL_PROGRESS, started: { ...EMPTY_COMBO_COUNTS }, done: { ...EMPTY_COMBO_COUNTS } },
  mctsLoopIter: 0,
  u2eIteration: 0, u2eFuncEvals: 0, u2eMaxFe: 5000, u2eHistory: [],
  u2eIterProgress: { ...EMPTY_U2E_ITER },
  u2eLoopIter: 0,
  outputDetails: [],
  paperSteps: {}, runError: null,
}

export const useRunStore = create<RunState>((set, get) => ({
  ...INITIAL,

  startRun: async (config) => {
    try {
      const res = await fetch('/api/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config),
      })
      if (!res.ok) {
        const text = await res.text()
        set({ runError: `启动失败 (${res.status}): ${text}` })
        return
      }
      const { run_id } = await res.json()
      set({ ...INITIAL, runId: run_id, phase: 1, progress: 5, u2eMaxFe: config.max_fe })
    } catch (err) {
      set({ runError: `网络错误: ${err instanceof Error ? err.message : String(err)}` })
    }
  },

  handleEvent: (event) => {
    set(state => {
      const next = { ...state }

      // 代码生成 map 子步骤判定（来自 arch_codegen_single.json 透传，Phase 3）
      const isCodegenMapSub = (id: string): id is CodegenMapSubStep =>
        (CODEGEN_MAP_SUB_STEPS as readonly string[]).includes(id) && (state.phase === 3 || state.phase === 1)
      // 验证修复子步骤判定（来自 arch_verify_and_fix.json 透传，Phase 3）
      const isVerifyFixSub = (id: string): id is VerifyFixSubStep =>
        (VERIFY_FIX_SUB_STEPS as readonly string[]).includes(id) && state.phase === 3
      // MCTS combo 子步骤判定（来自 mcts_single_combo.json 透传）
      const isMctsComboSub = (id: string): id is MctsComboSubStep =>
        (MCTS_COMBO_SUB_STEPS as readonly string[]).includes(id) && state.phase === 4
      // U2E body 子步骤判定（来自 u2e_optimize loop body 透传）
      const isU2eBodySub = (id: string): id is U2eBodySubStep =>
        (U2E_BODY_SUB_STEPS as readonly string[]).includes(id) && (state.phase === 5 || next.phase === 5)

      switch (event.type) {
        case 'step_start': {
          next.stepStatus = { ...state.stepStatus, [event.step_id]: event.skipped ? 'done' : 'running' }
          if (PAPER_STEP_IDS.includes(event.step_id)) {
            next.paperSteps = { ...state.paperSteps, [event.step_id]: event.skipped ? 'done' : 'running' }
          }
          // 记录步骤开始时间（跳过的步骤不记录耗时）
          if (!event.skipped) {
            next.stepTimings = { ...state.stepTimings, [event.step_id]: { startedAt: Date.now() } }
          }
          const phase = inferPhase(next.stepStatus)
          next.phase = phase
          next.progress = phaseProgress(phase)

          // ── 代码生成 map 子步骤：累计 started 计数 ──
          if (isCodegenMapSub(event.step_id)) {
            const prev = state.codegenMapProgress
            next.codegenMapProgress = {
              ...prev,
              started: { ...prev.started, [event.step_id]: prev.started[event.step_id] + 1 },
            }
          }
          // ── 验证修复子步骤 ──
          if (isVerifyFixSub(event.step_id)) {
            next.verifyFixStatus = { ...state.verifyFixStatus, [event.step_id]: 'running' }
          }
          // ── MCTS combo 子步骤：累计 started 计数 ──
          if (isMctsComboSub(event.step_id)) {
            const prev = state.mctsEvalProgress
            next.mctsEvalProgress = {
              ...prev,
              started: { ...prev.started, [event.step_id]: prev.started[event.step_id] + 1 },
            }
          }
          // ── MCTS fanout 开始：获取 beam 数（重置 eval progress） ──
          if (event.step_id === 'expand' && state.phase === 4) {
            const beamCount = state.mctsEvalProgress.beamCount || state.mctsFrontier.length || 3
            next.mctsEvalProgress = {
              started: { ...EMPTY_COMBO_COUNTS },
              done:    { ...EMPTY_COMBO_COUNTS },
              beamCount,
            }
          }
          // ── U2E body 子步骤 ──
          if (isU2eBodySub(event.step_id)) {
            next.u2eIterProgress = { ...state.u2eIterProgress, [event.step_id]: 'running' }
          }
          break
        }
        case 'step_done': {
          next.stepStatus  = { ...state.stepStatus,  [event.step_id]: 'done' }
          next.stepOutputs = { ...state.stepOutputs, [event.step_id]: event.outputs }
          // 记录步骤结束时间
          const existing = state.stepTimings[event.step_id]
          if (existing && !existing.finishedAt) {
            next.stepTimings = { ...state.stepTimings, [event.step_id]: { ...existing, finishedAt: Date.now() } }
          }
          const phase = inferPhase(next.stepStatus)
          next.phase = phase
          next.progress = phaseProgress(phase)

          if (event.step_id === 'baseline') {
            const o = event.outputs as Record<string, number>
            next.metrics = { ...next.metrics, baseline: o.score }
          }
          if (event.step_id === 'mcts_optimize') {
            const best = (event.outputs as Record<string, unknown>).best_node as MctsNode | undefined
            if (best) next.metrics = { ...next.metrics, mcts_best: best.score }
          }
          if (event.step_id === 'tree_text') {
            const o = event.outputs as Record<string, unknown>
            next.mctsNodes      = (o.all_nodes ?? {}) as Record<string, MctsNode>
            next.mctsFrontier   = (o.frontier  ?? []) as string[]
            next.mctsGeneration = (o.generation ?? 0) as number
            const bestNode = o.best_node as MctsNode | null
            if (bestNode) next.metrics = { ...next.metrics, mcts_best: bestNode.score }
          }
          // ── 代码生成 map 子步骤：累计 done 计数 ──
          if (isCodegenMapSub(event.step_id)) {
            const prev = next.codegenMapProgress ?? state.codegenMapProgress
            next.codegenMapProgress = {
              ...prev,
              done: { ...prev.done, [event.step_id]: prev.done[event.step_id] + 1 },
            }
          }
          // build_codegen_tasks 完成：获取 totalFns
          if (event.step_id === 'build_codegen_tasks') {
            const tasks = (event.outputs as Record<string, unknown>).tasks as unknown[] | undefined
            if (tasks) {
              next.codegenMapProgress = {
                ...state.codegenMapProgress,
                started: { ...EMPTY_CODEGEN_COUNTS },
                done:    { ...EMPTY_CODEGEN_COUNTS },
                totalFns: tasks.length,
              }
            }
          }
          // ── 验证修复子步骤 ──
          if (isVerifyFixSub(event.step_id)) {
            next.verifyFixStatus = { ...state.verifyFixStatus, [event.step_id]: 'done' }
          }
          // MCTS fanout 完成：从输出获取 beam 数
          if (event.step_id === 'fanout' && state.phase === 4) {
            const tasks = (event.outputs as Record<string, unknown>).tasks as unknown[] | undefined
            if (tasks) {
              next.mctsEvalProgress = { ...state.mctsEvalProgress, beamCount: tasks.length }
            }
          }
          // ── MCTS combo 子步骤：累计 done 计数 ──
          if (isMctsComboSub(event.step_id)) {
            const prev = next.mctsEvalProgress ?? state.mctsEvalProgress
            next.mctsEvalProgress = {
              ...prev,
              done: { ...prev.done, [event.step_id]: prev.done[event.step_id] + 1 },
            }
          }
          // U2E: 累积真实历史数据
          if (event.step_id === 'update_state') {
            const o = event.outputs as Record<string, unknown>
            const iteration = (o.iteration      as number) ?? next.u2eIteration
            const funcEvals = (o.function_evals  as number) ?? next.u2eFuncEvals
            const bestObj   = o.best_obj_overall as number | null
            next.u2eIteration = iteration
            next.u2eFuncEvals = funcEvals
            if (bestObj != null) {
              next.metrics = { ...next.metrics, u2e_best: bestObj }
              next.u2eHistory = [...state.u2eHistory, { iteration, function_evals: funcEvals, best_obj: bestObj }]
            }
          }
          // ── U2E body 子步骤 ──
          if (isU2eBodySub(event.step_id)) {
            next.u2eIterProgress = { ...state.u2eIterProgress, [event.step_id]: 'done' }
          }
          if (PAPER_STEP_IDS.includes(event.step_id)) {
            next.paperSteps = { ...state.paperSteps, [event.step_id]: 'done' }
          }
          break
        }
        case 'step_error': {
          next.stepStatus = { ...state.stepStatus, [event.step_id]: 'error' }
          next.stepLogs = {
            ...state.stepLogs,
            [event.step_id]: [...(state.stepLogs[event.step_id] ?? []), `ERROR: ${event.error}`],
          }
          if (PAPER_STEP_IDS.includes(event.step_id)) {
            next.paperSteps = { ...state.paperSteps, [event.step_id]: 'error' }
          }
          // ── 代码生成 map 子步骤 error ──
          if (isCodegenMapSub(event.step_id)) {
            const prev = state.codegenMapProgress
            next.codegenMapProgress = {
              ...prev,
              done: { ...prev.done, [event.step_id]: prev.done[event.step_id] + 1 },
            }
          }
          // ── 验证修复子步骤 error ──
          if (isVerifyFixSub(event.step_id)) {
            next.verifyFixStatus = { ...state.verifyFixStatus, [event.step_id]: 'error' }
          }
          // ── MCTS combo 子步骤 error ──
          if (isMctsComboSub(event.step_id)) {
            const prev = state.mctsEvalProgress
            next.mctsEvalProgress = {
              ...prev,
              done: { ...prev.done, [event.step_id]: prev.done[event.step_id] + 1 },
            }
          }
          // ── U2E body 子步骤 error ──
          if (isU2eBodySub(event.step_id)) {
            next.u2eIterProgress = { ...state.u2eIterProgress, [event.step_id]: 'error' }
          }
          break
        }
        case 'loop_jump': {
          // 架构验证 loop_to 回跳（validate_module_imports → module_split，validate_file_imports → file_split_raw）
          if (event.to_step === 'module_split' || event.to_step === 'file_split_raw') {
            next.archRetries = {
              ...state.archRetries,
              [event.to_step]: (state.archRetries[event.to_step] ?? 0) + 1,
            }
          }
          // 验证修复 fix_loop 回跳：重置子步骤状态
          if (event.to_step === 'collect_context') {
            next.verifyFixIter = event.iteration
            next.verifyFixStatus = { ...EMPTY_VERIFY_FIX }
          }
          // MCTS gen_loop 回跳：重置 eval progress，更新迭代计数
          if (event.to_step === 'tree_text') {
            next.mctsLoopIter = event.iteration
            next.mctsEvalProgress = {
              started: { ...EMPTY_COMBO_COUNTS },
              done:    { ...EMPTY_COMBO_COUNTS },
              beamCount: state.mctsEvalProgress.beamCount,
            }
          }
          // U2E loop 回跳：重置迭代子步骤状态
          if (event.to_step === 'build_func_iter_tasks') {
            next.u2eLoopIter = event.iteration
            next.u2eIterProgress = { ...EMPTY_U2E_ITER }
          }
          break
        }
        case 'gate_waiting': {
          next.gateState = event.payload
          // 自动跳转到对应 phase，防止用户停留在其他页面看不到 gate 弹窗
          if (event.payload.gate_type === 'arch') {
            next.phase = 2
            next.progress = phaseProgress(2)
          } else if (event.payload.gate_type === 'mcts') {
            next.phase = 4
            next.progress = phaseProgress(4)
          }
          break
        }
        case 'log_line': {
          const existing = state.stepLogs[event.step_id] ?? []
          next.stepLogs = {
            ...state.stepLogs,
            [event.step_id]: [...existing, event.text].slice(-500),
          }
          break
        }
        case 'llm_output': {
          const detail: StepOutputDetail = {
            type: 'llm', stepId: event.step_id, ref: event.ref,
            output: event.output, ts: Date.now(),
          }
          // 最多保留 200 条，防止内存膨胀
          next.outputDetails = [...state.outputDetails.slice(-199), detail]
          break
        }
        case 'tool_output': {
          const detail: StepOutputDetail = {
            type: 'tool', stepId: event.step_id, ref: event.ref,
            output: event.output, params: event.params, ts: Date.now(),
          }
          next.outputDetails = [...state.outputDetails.slice(-199), detail]
          break
        }
        case 'metrics_update': {
          next.metrics = { ...state.metrics, ...event.metrics }
          break
        }
        case 'run_done': {
          next.phase = 7
          next.progress = 100
          next.gateState = null
          break
        }
        case 'run_error': {
          next.runError = event.error
          break
        }
      }
      return next
    })
  },

  submitGate: async (gateId, payload) => {
    const { runId } = get()
    if (!runId) return
    try {
      const res = await fetch(`/api/runs/${runId}/gate/${gateId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!res.ok) {
        const text = await res.text()
        set({ runError: `Gate 提交失败 (${res.status}): ${text}` })
        return
      }
      set({ gateState: null })
    } catch (err) {
      set({ runError: `Gate 提交网络错误: ${err instanceof Error ? err.message : String(err)}` })
    }
  },

  abortRun: async () => {
    const { runId } = get()
    if (!runId) return
    try { await fetch(`/api/runs/${runId}/abort`, { method: 'POST' }) } catch { /* ignore */ }
    set({ runError: '运行已中止', gateState: null })
  },

  reset: () => set(INITIAL),
  tickElapsed: () => set(s => s.wsConnected ? { elapsed: s.elapsed + 1 } : {}),
  setWsConnected: (connected: boolean) => set({ wsConnected: connected }),
}))
