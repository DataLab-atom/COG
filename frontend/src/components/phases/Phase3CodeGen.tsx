import { useRunStore } from '@/store/runStore'
import type { StepStatus, CodegenMapProgress, VerifyFixSubStep } from '@/types'
import { CODEGEN_MAP_SUB_STEPS, VERIFY_FIX_SUB_STEPS } from '@/types'
import { StepRow, Card, StepIcon } from '@/components/ui'
import OutputDetailPanel from '@/components/OutputDetailPanel'
import { clsx } from 'clsx'

const CODEGEN_STEPS: { id: string; label: string; isMap?: boolean }[] = [
  { id: 'scaffold',           label: '生成项目脚手架' },
  { id: 'build_codegen_tasks', label: '构建代码生成任务' },
  { id: 'fn_codegen',         label: '并行函数体生成', isMap: true },
  { id: 'collect_results',    label: '汇总生成结果' },
  { id: 'assemble',           label: 'AST 组装写入文件' },
  { id: 'syntax_check',       label: 'Python 语法检查' },
  { id: 'verify_and_fix',     label: '沙盒验证与修复' },
  { id: 'enrich_tree',        label: '回填架构树' },
]

const CODEGEN_SUB_LABELS: Record<string, string> = {
  prepare: '变量准备',
  codegen: 'LLM 生成',
}

const VERIFY_FIX_LABELS: Record<string, string> = {
  initial_run:     '首次沙盒运行',
  collect_context: '收集错误上下文',
  fix_agent:       'LLM 生成修复',
  apply_fix:       '应用修复 patch',
  rerun:           '重新运行验证',
  check_state:     '检查修复结果',
}

/* ── fn_codegen 子图进度条 ──────────────────────────────────────────────────── */

function CodegenProgress({ progress }: { progress: CodegenMapProgress }) {
  const { started, done, totalFns } = progress
  const hasAny = CODEGEN_MAP_SUB_STEPS.some(s => started[s] > 0)
  if (!hasAny || totalFns === 0) return null

  return (
    <Card className="mt-2 mb-2">
      <div className="text-[11px] text-muted mb-2">
        函数体生成子步骤进度 ({totalFns} 个函数, 依赖感知并行)
      </div>
      <div className="flex flex-col gap-1">
        {CODEGEN_MAP_SUB_STEPS.map(step => {
          const d = done[step]
          const pct = Math.round((d / totalFns) * 100)
          const isActive = started[step] > d
          const isDone = d >= totalFns
          return (
            <div key={step} className="flex items-center gap-2">
              <span className="text-[11px] w-20 shrink-0 text-secondary">{CODEGEN_SUB_LABELS[step]}</span>
              <div className="flex-1 h-1.5 bg-elevated rounded-sm overflow-hidden">
                <div
                  className={clsx('h-full rounded-sm transition-all duration-300',
                    isDone ? 'bg-teal' : isActive ? 'bg-blue' : 'bg-blue/40')}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <span className={clsx('text-[10px] font-mono w-12 text-right',
                isDone ? 'text-teal' : isActive ? 'text-blue' : 'text-muted')}>
                {d}/{totalFns}
              </span>
            </div>
          )
        })}
      </div>
    </Card>
  )
}

/* ── 沙盒验证修复流水线 ────────────────────────────────────────────────────── */

function VerifyFixPipeline({ status, iter }: {
  status: Record<VerifyFixSubStep, StepStatus>; iter: number
}) {
  const hasAny = VERIFY_FIX_SUB_STEPS.some(s => status[s] !== 'pending')
  if (!hasAny) return null

  // fix_loop body 步骤（collect_context 之后的）
  const loopSteps = VERIFY_FIX_SUB_STEPS.filter(s => s !== 'initial_run')
  const showLoop = loopSteps.some(s => status[s] !== 'pending')

  return (
    <Card className="mt-2 mb-2">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[11px] text-muted">沙盒验证与修复子步骤</div>
        {iter > 0 && <span className="text-[10px] text-amber font-mono">修复轮次 #{iter}</span>}
      </div>

      {/* initial_run */}
      <div className="flex items-center gap-1.5 mb-1.5">
        <StepIcon status={status['initial_run']} size={12} />
        <span className={clsx('text-[12px]',
          status['initial_run'] === 'done' ? 'text-teal' :
          status['initial_run'] === 'running' ? 'text-blue' : 'text-muted')}>
          {VERIFY_FIX_LABELS['initial_run']}
        </span>
        {status['initial_run'] === 'done' && (
          <span className="text-[10px] text-secondary ml-auto">
            {showLoop ? '需要修复' : '等待结果...'}
          </span>
        )}
      </div>

      {/* fix_loop body 步骤 */}
      {showLoop && (
        <div className="ml-4 pl-3 border-l-2 border-amber/30">
          <div className="flex items-center gap-1.5 flex-wrap">
            {loopSteps.map((step, i) => (
              <div key={step} className="contents">
                <div className={clsx(
                  'flex items-center gap-1 px-2 py-1 rounded text-[11px] transition-all',
                  status[step] === 'running' ? 'bg-blue/15 text-blue font-semibold' :
                  status[step] === 'done'    ? 'bg-teal/10 text-teal' :
                  status[step] === 'error'   ? 'bg-danger/10 text-danger' :
                  'bg-elevated text-muted'
                )}>
                  <StepIcon status={status[step]} size={10} />
                  {VERIFY_FIX_LABELS[step]}
                </div>
                {i < loopSteps.length - 1 && <span className="text-strong text-[10px]">&rarr;</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  )
}

/* ── 主组件 ───────────────────────────────────────────────────────────────── */

/** 代码生成阶段涉及的步骤 ID + 子图内部步骤 ID */
const CODEGEN_STEP_IDS = [
  ...CODEGEN_STEPS.map(s => s.id),
  // arch_codegen_single 子步骤
  'prepare', 'codegen',
  // arch_verify_and_fix 子步骤
  'initial_run', 'collect_context', 'fix_agent', 'apply_fix', 'rerun', 'check_state',
  // init_state
  'init_state',
]

export default function Phase3CodeGen() {
  const stepStatus         = useRunStore(s => s.stepStatus)
  const stepOutputs        = useRunStore(s => s.stepOutputs)
  const codegenMapProgress = useRunStore(s => s.codegenMapProgress)
  const verifyFixStatus    = useRunStore(s => s.verifyFixStatus)
  const verifyFixIter      = useRunStore(s => s.verifyFixIter)
  const outputDetails      = useRunStore(s => s.outputDetails)

  const getStatus = (id: string): StepStatus => stepStatus[id] ?? 'pending'

  const tasksOutput = stepOutputs['build_codegen_tasks'] as Record<string, unknown> | undefined
  const taskCount = (tasksOutput?.tasks as unknown[] | undefined)?.length

  const verifyOutput = stepOutputs['verify_and_fix'] as Record<string, unknown> | undefined
  const verifyOk = verifyOutput?.ok as boolean | undefined

  // 语法检查错误
  const syntaxOutput = stepOutputs['syntax_check'] as Record<string, unknown> | undefined
  const syntaxErrors = (syntaxOutput?.errors as string[]) ?? []
  const syntaxOk = syntaxOutput?.ok as boolean | undefined

  // scaffold 输出
  const scaffoldOutput = stepOutputs['scaffold'] as Record<string, unknown> | undefined
  const skeletonCount = scaffoldOutput?.skeletons ? Object.keys(scaffoldOutput.skeletons as Record<string, unknown>).length : undefined
  const stubCount = scaffoldOutput?.fn_stubs ? Object.keys(scaffoldOutput.fn_stubs as Record<string, unknown>).length : undefined

  // assemble 写入文件
  const assembleOutput = stepOutputs['assemble'] as Record<string, unknown> | undefined
  const writtenFiles = assembleOutput?.written as string[] | undefined

  const doneCount = CODEGEN_STEPS.filter(s => getStatus(s.id) === 'done').length
  const pct = Math.round((doneCount / CODEGEN_STEPS.length) * 100)

  function getExtra(step: { id: string; isMap?: boolean }): React.ReactNode {
    const status = getStatus(step.id)
    switch (step.id) {
      case 'scaffold':
        if (status === 'done' && skeletonCount != null) {
          return <span className="text-xs text-secondary font-mono">{skeletonCount} 文件, {stubCount} 函数桩</span>
        }
        break
      case 'fn_codegen':
        if (status === 'running') {
          const d = codegenMapProgress.done.codegen
          const t = codegenMapProgress.totalFns
          if (t > 0) return <span className="text-xs text-blue font-mono">{d}/{t} 完成</span>
          return <span className="text-muted text-xs font-mono">并行处理中...</span>
        }
        if (status === 'done' && codegenMapProgress.totalFns > 0) {
          return <span className="text-xs text-teal font-mono">{codegenMapProgress.totalFns} 函数已生成</span>
        }
        break
      case 'assemble':
        if (status === 'done' && writtenFiles) {
          return <span className="text-xs text-secondary font-mono">{writtenFiles.length} 文件写入</span>
        }
        break
      case 'syntax_check':
        if (status === 'done') {
          return syntaxOk
            ? <span className="text-teal text-xs">全部通过</span>
            : <span className="text-danger text-xs">{syntaxErrors.length} 错误</span>
        }
        break
      case 'verify_and_fix':
        if (status === 'done') {
          return verifyOk
            ? <span className="text-teal text-xs">沙盒验证通过</span>
            : <span className="text-danger text-xs">验证失败</span>
        }
        if (status === 'running') {
          return verifyFixIter > 0
            ? <span className="text-amber text-xs">修复轮次 #{verifyFixIter}</span>
            : <span className="text-blue text-xs">运行中...</span>
        }
        break
    }
    if (step.isMap && status === 'running') {
      return <span className="text-muted text-xs font-mono">并行处理中...</span>
    }
    return undefined
  }

  return (
    <div className="max-w-3xl mx-auto">
      <h3 className="text-primary mb-4">代码生成</h3>

      {/* Progress bar */}
      <Card className="mb-4">
        <div className="flex items-center gap-4">
          <div className="flex-1 h-2 bg-elevated rounded-sm overflow-hidden">
            <div className="h-full bg-blue rounded-sm transition-all duration-500" style={{ width: `${pct}%` }} />
          </div>
          <span className="text-muted text-xs font-mono shrink-0">{doneCount}/{CODEGEN_STEPS.length}</span>
        </div>
        {taskCount != null && (
          <div className="text-secondary text-xs mt-2">
            共 {taskCount} 个函数待生成（依赖感知并行）
          </div>
        )}
      </Card>

      {/* Step list */}
      <div className="flex flex-col gap-1.5">
        {CODEGEN_STEPS.map(step => (
          <div key={step.id}>
            <StepRow status={getStatus(step.id)} label={step.label} extra={getExtra(step)} />
            {/* fn_codegen 展开子图进度 */}
            {step.id === 'fn_codegen' && getStatus('fn_codegen') === 'running' && (
              <CodegenProgress progress={codegenMapProgress} />
            )}
            {/* syntax_check 错误列表 */}
            {step.id === 'syntax_check' && syntaxErrors.length > 0 && (
              <div className="mt-1.5 ml-8 flex flex-col gap-1 max-h-[120px] overflow-y-auto">
                {syntaxErrors.map((e, i) => (
                  <div key={i} className="px-3 py-1.5 bg-danger/10 rounded text-[11px] text-danger font-mono">{e}</div>
                ))}
              </div>
            )}
            {/* verify_and_fix 子图进度 */}
            {step.id === 'verify_and_fix' && getStatus('verify_and_fix') === 'running' && (
              <VerifyFixPipeline status={verifyFixStatus} iter={verifyFixIter} />
            )}
          </div>
        ))}
      </div>

      {/* LLM / 工具输出详情 */}
      <OutputDetailPanel
        details={outputDetails}
        filterStepIds={CODEGEN_STEP_IDS}
        title="代码生成 LLM / 工具输出"
      />
    </div>
  )
}
