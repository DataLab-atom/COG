// ── 运行状态 ─────────────────────────────────────────────────────────────────

export type StepStatus = 'pending' | 'running' | 'done' | 'error'

export type Phase =
  | 0  // 启动配置
  | 1  // 架构生成
  | 2  // 架构审核 Gate
  | 3  // 代码生成
  | 4  // 策略搜索（树搜索优化）
  | 5  // 进化优化（种群演化）
  | 6  // 论文写作
  | 7  // 完成

export const PHASE_LABELS: Record<Phase, string> = {
  0: '启动配置',
  1: '架构生成',
  2: '架构审核',
  3: '代码生成',
  4: '策略搜索',
  5: '进化优化',
  6: '论文写作',
  7: '完成',
}

// ── 断点信息 ──────────────────────────────────────────────────────────────────

export interface CheckpointEntry {
  run_id:          string
  checkpoint_dir:  string
  completed_steps: string[]
  step_count:      number
  last_updated:    number
}

// ── 事件类型（来自 WebSocket）─────────────────────────────────────────────────

export type WsEvent =
  | { type: 'step_start';     step_id: string; skipped?: boolean }
  | { type: 'step_done';      step_id: string; outputs: Record<string, unknown>; skipped?: boolean }
  | { type: 'step_error';     step_id: string; error: string }
  | { type: 'loop_jump';      from_step: string; to_step: string; iteration: number; carry: Record<string, unknown> }
  | { type: 'gate_waiting';   gate_id: string; payload: GatePayload }
  | { type: 'log_line';       step_id: string; text: string }
  | { type: 'llm_output';     step_id: string; ref: string; output: unknown }
  | { type: 'tool_output';    step_id: string; ref: string; params: Record<string, unknown>; output: unknown }
  | { type: 'run_done';       outputs: Record<string, unknown> }
  | { type: 'run_error';      error: string }
  | { type: 'metrics_update'; metrics: Metrics }

/** LLM / 工具输出详情记录 */
export interface StepOutputDetail {
  type:    'llm' | 'tool'
  stepId:  string
  ref:     string
  output:  unknown
  params?: Record<string, unknown>
  ts:      number
}

// ── MCTS ──────────────────────────────────────────────────────────────────────

export interface MctsNode {
  node_id:      string
  generation:   number
  parent_id:    string | null
  op:           string | null
  angle:        string | null
  patches:      Patch[]
  score:        number
  metrics:      Record<string, unknown>
  output_log:   string
  tried_combos: [string, string][]
}

export interface Patch {
  action:      string
  target_file: string
  target_type: string
  target_name: string
  code:        string
}

export interface GatePayload {
  gate_id:    string
  gate_type:  'arch' | 'mcts'
  generation?: number
  tree_text?:  string
  new_nodes?:  MctsNode[]
  arch_tree?:  ArchNode[]
  arch_ok?:    boolean
  arch_errors?: string[]
}

export type MctsAction = 'continue' | 'select' | 'rollback' | 'architecture' | 'stop'
export type NextMode   = 'llm' | 'random'

// ── 架构树 ────────────────────────────────────────────────────────────────────

export interface ArchNode {
  id:       string
  kind:     'module' | 'file' | 'function' | 'class'
  name:     string
  path?:    string
  code?:    string
  imports?: string[]
  calls?:   string[]
  children?: ArchNode[]
}

// ── U2E ───────────────────────────────────────────────────────────────────────

export interface FuncState {
  func_name:  string
  best_score: number | null
  cur_score:  number | null
  history:    number[]
  status:     'idle' | 'evolving' | 'done'
}

/** U2E 每轮快照数据点 */
export interface U2eHistoryPoint {
  iteration:      number
  function_evals: number
  best_obj:       number
}

// ── 子图子步骤追踪 ──────────────────────────────────────────────────────────────

/** MCTS 单 combo 评估子步骤 ID（来自 mcts_single_combo.json 透传事件） */
export const MCTS_COMBO_SUB_STEPS = ['critic', 'parse_atomic_ops', 'engineer_tasks', 'collect_patches', 'sandbox'] as const
export type MctsComboSubStep = typeof MCTS_COMBO_SUB_STEPS[number]

/** MCTS 每代评估子步骤的聚合计数 */
export interface MctsEvalProgress {
  /** 每个子步骤已开始的次数 */
  started: Record<MctsComboSubStep, number>
  /** 每个子步骤已完成的次数 */
  done:    Record<MctsComboSubStep, number>
  /** 当前 beam 总数（从 fanout 输出推断） */
  beamCount: number
}

/** 代码生成 fn_codegen map 子步骤（来自 arch_codegen_single.json 透传事件） */
export const CODEGEN_MAP_SUB_STEPS = ['prepare', 'codegen'] as const
export type CodegenMapSubStep = typeof CODEGEN_MAP_SUB_STEPS[number]

export interface CodegenMapProgress {
  started: Record<CodegenMapSubStep, number>
  done:    Record<CodegenMapSubStep, number>
  totalFns: number
}

/** 沙盒验证修复子步骤 ID（来自 arch_verify_and_fix.json 透传事件） */
export const VERIFY_FIX_SUB_STEPS = ['initial_run', 'collect_context', 'fix_agent', 'apply_fix', 'rerun', 'check_state'] as const
export type VerifyFixSubStep = typeof VERIFY_FIX_SUB_STEPS[number]

/** U2E 循环体子步骤 ID（来自 arch_then_mcts_then_u2e.json 透传事件） */
export const U2E_BODY_SUB_STEPS = ['build_func_iter_tasks', 'func_evolve', 'collect_results', 'update_state'] as const
export type U2eBodySubStep = typeof U2E_BODY_SUB_STEPS[number]

/** U2E 当前迭代的子步骤状态 */
export type U2eIterProgress = Record<U2eBodySubStep, StepStatus>

// ── 全局指标 ──────────────────────────────────────────────────────────────────

export interface Metrics {
  baseline?:         number
  mcts_best?:        number
  u2e_best?:         number
  function_evals?:   number
  mcts_generations?: number
  u2e_iterations?:   number
}

// ── 配置表单 ──────────────────────────────────────────────────────────────────

export interface RunConfig {
  requirement:            string
  output_dir:             string
  sandbox_timeout:        number
  beam_width:             number
  consecutive_bad_limit:  number
  max_generations:        number
  pop_size:               number
  mutation_rate:          number
  max_fe:                 number
  user_generator_prompt:  string
  external_knowledge:     string
  existing_project_root:  string
  existing_direction:     string
  auto_approve_arch:      boolean
  auto_approve_mcts:      boolean
  tex_template_path:      string
  reference_bib_path:     string
  expand_references:      boolean
  s2_api_key:             string
  unpaywall_email:        string
  openai_api_key:         string
  pdf_md_token:           string
  embedding_api_key:      string
  text_model_name:        string
  write_model_name:       string
  embedding_model:        string
  embedding_model_source: string
  reranker_path:          string
  reranker_model_source:  string
  database_device:        string
  pdf_transform_mode:     string
  all_paper_num:          number
  resume_checkpoint_dir:  string
}

export const DEFAULT_CONFIG: RunConfig = {
  requirement:            '',
  output_dir:             '',
  sandbox_timeout:        300,
  beam_width:             3,
  consecutive_bad_limit:  3,
  max_generations:        50,
  pop_size:               10,
  mutation_rate:          0.3,
  max_fe:                 5000,
  user_generator_prompt:  '',
  external_knowledge:     '',
  existing_project_root:  '',
  existing_direction:     'maximize',
  auto_approve_arch:      false,
  auto_approve_mcts:      false,
  tex_template_path:      '',
  reference_bib_path:     '',
  expand_references:      true,
  s2_api_key:             '',
  unpaywall_email:        '',
  openai_api_key:         '',
  pdf_md_token:           '',
  embedding_api_key:      '',
  text_model_name:        'claude-sonnet-4-6',
  write_model_name:       'claude-sonnet-4-6',
  embedding_model:        'BAAI/bge-m3',
  embedding_model_source: 'api',
  reranker_path:          'BAAI/bge-reranker-v2-m3',
  reranker_model_source:  'api',
  database_device:        'cpu',
  pdf_transform_mode:     'api',
  all_paper_num:          70,
  resume_checkpoint_dir:  '',
}
