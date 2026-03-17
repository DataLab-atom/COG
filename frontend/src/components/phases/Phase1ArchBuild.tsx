import { useState, useEffect } from 'react'
import { useRunStore } from '@/store/runStore'
import type { StepStatus } from '@/types'
import { StepRow, Button, Modal, Badge, inputClass } from '@/components/ui'
import OutputDetailPanel from '@/components/OutputDetailPanel'

const ARCH_STEPS: { id: string; label: string; isMap?: boolean }[] = [
  { id: 'validation',              label: '需求验证' },
  { id: 'module_split',            label: '模块划分' },
  { id: 'validate_module_imports', label: '验证模块导入' },
  { id: 'file_split_raw',          label: '文件划分',   isMap: true },
  { id: 'file_backfill',           label: '文件回填' },
  { id: 'validate_file_imports',   label: '验证文件导入' },
  { id: 'leaf_define_raw',         label: '叶子节点定义', isMap: true },
  { id: 'leaf_backfill',           label: '叶子回填' },
  { id: 'internal_fanout',         label: '内部展开' },
  { id: 'resolve_needs',           label: '解析依赖' },
  { id: 'backfill',                label: '最终回填' },
  { id: 'static_check',            label: '静态检查' },
]

function renderTree(nodes: Record<string, unknown>[], depth = 0): React.ReactNode {
  return nodes.map((node, i) => {
    const icon = node.kind === 'module' ? '\uD83D\uDCC1' : node.kind === 'file' ? '\uD83D\uDCC4' : '\u26A1'
    return (
      <div key={i}>
        <div style={{ paddingLeft: depth * 16 }}>{icon} {String(node.name ?? node.id ?? '')}</div>
        {Array.isArray(node.children) && node.children.length > 0 && renderTree(node.children as Record<string, unknown>[], depth + 1)}
      </div>
    )
  })
}

/** 根据步骤 ID 和输出数据生成摘要信息 */
function stepExtra(
  id: string,
  status: StepStatus,
  outputs: Record<string, Record<string, unknown>>,
  archRetries: Record<string, number>,
  isMap?: boolean,
): React.ReactNode {
  if (status === 'running' && isMap) {
    return <span className="text-muted text-xs font-mono">并行处理中...</span>
  }

  const out = outputs[id]
  if (!out || status === 'pending') return undefined

  switch (id) {
    case 'validation': {
      const mn = out.metric_name as string | undefined
      const dir = out.direction as string | undefined
      if (mn) return (
        <span className="text-xs text-teal font-mono">
          {mn} ({dir === 'minimize' ? '\u2193' : '\u2191'})
        </span>
      )
      break
    }
    case 'module_split': {
      const result = out.result as unknown[] | undefined
      if (result) return (
        <span className="text-xs text-secondary font-mono">{result.length} 个模块</span>
      )
      break
    }
    case 'validate_module_imports': {
      const ok = out.ok as boolean | undefined
      const errors = out.errors as string[] | undefined
      const retries = archRetries['module_split'] ?? 0
      return (
        <span className="text-xs font-mono">
          {ok ? <span className="text-teal">DAG 无环</span> : <span className="text-danger">{errors?.length ?? 0} 个环</span>}
          {retries > 0 && <span className="text-amber ml-1.5">(重试 {retries})</span>}
        </span>
      )
    }
    case 'file_split_raw': {
      // map 输出为 list of results，其中每个是文件列表
      const mapResult = out as unknown
      if (Array.isArray(mapResult)) {
        const fileCount = (mapResult as Record<string, unknown>[]).reduce((sum, item) => {
          const result = item?.result as unknown[] | undefined
          return sum + (result?.length ?? 0)
        }, 0)
        if (fileCount > 0) return <span className="text-xs text-secondary font-mono">{fileCount} 个文件</span>
      }
      break
    }
    case 'validate_file_imports': {
      const ok = out.ok as boolean | undefined
      const errors = out.errors as string[] | undefined
      const retries = archRetries['file_split_raw'] ?? 0
      return (
        <span className="text-xs font-mono">
          {ok ? <span className="text-teal">DAG 无环</span> : <span className="text-danger">{errors?.length ?? 0} 个环</span>}
          {retries > 0 && <span className="text-amber ml-1.5">(重试 {retries})</span>}
        </span>
      )
    }
    case 'leaf_backfill': {
      const files = out.files as Record<string, unknown>[] | undefined
      if (files) {
        const fnCount = files.reduce((sum, f) => sum + ((f.functions as unknown[])?.length ?? 0), 0)
        const typeCount = files.reduce((sum, f) => sum + ((f.types as unknown[])?.length ?? 0), 0)
        return <span className="text-xs text-secondary font-mono">{fnCount} 函数, {typeCount} 类型</span>
      }
      break
    }
    case 'static_check': {
      const ok = out.ok as boolean | undefined
      const errors = out.errors as string[] | undefined
      const warnings = out.warnings as string[] | undefined
      return (
        <span className="text-xs font-mono">
          {ok
            ? <span className="text-teal">通过</span>
            : <span className="text-danger">{errors?.length ?? 0} 错误</span>}
          {(warnings?.length ?? 0) > 0 && <span className="text-amber ml-1.5">{warnings!.length} 警告</span>}
        </span>
      )
    }
  }
  return undefined
}

export function ArchGateModal() {
  const gateState  = useRunStore(s => s.gateState)
  const submitGate = useRunStore(s => s.submitGate)
  const [feedback, setFeedback] = useState('')
  const [countdown, setCountdown] = useState<number | null>(null)

  useEffect(() => {
    if (countdown === null) return
    if (countdown === 0) { submitGate('arch_gate', {}); return }
    const t = setTimeout(() => setCountdown(c => c !== null ? c - 1 : null), 1000)
    return () => clearTimeout(t)
  }, [countdown, submitGate])

  if (!gateState) return null

  return (
    <Modal open={true} onClose={() => {}} maxWidth="900px">
      <div className="text-amber text-lg font-bold mb-5">需要你的审核</div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
        <div>
          <div className="text-secondary font-semibold mb-3 text-[13px]">项目架构树</div>
          <div className="bg-elevated rounded-lg p-4 font-mono text-xs text-primary leading-relaxed min-h-[200px]">
            {gateState.arch_tree
              ? renderTree(gateState.arch_tree as unknown as Record<string, unknown>[])
              : <span className="text-muted">加载架构树...</span>}
          </div>
        </div>
        <div>
          <div className="text-secondary font-semibold mb-3 text-[13px]">架构说明</div>
          <div className="flex flex-col gap-3">
            <Badge color={gateState.arch_ok ? 'teal' : 'danger'}>
              {gateState.arch_ok ? '静态检查通过' : '存在错误，建议提交修改意见'}
            </Badge>
            {gateState.arch_errors?.map((e, i) => (
              <div key={i} className="px-3.5 py-2 bg-danger/10 rounded-md text-xs text-danger font-mono">{e}</div>
            ))}
          </div>
        </div>
      </div>

      <div className="mb-5">
        <label className="block text-secondary mb-2 text-[13px]">修改意见（留空表示批准）</label>
        <textarea value={feedback} onChange={e => setFeedback(e.target.value)}
          placeholder="例：请把 lr_scheduler 单独提取为一个文件..."
          className={`${inputClass} min-h-[80px] resize-y font-sans`} />
      </div>

      <div className="flex gap-3 justify-end">
        <Button variant="ghost" onClick={() => submitGate('arch_gate', { feedback })} disabled={!feedback.trim()}
          className="btn-ghost-amber" style={{ borderColor: 'var(--color-amber)', color: feedback.trim() ? 'var(--color-amber)' : undefined }}>
          提交修改意见
        </Button>
        <Button variant="teal" onClick={() => setCountdown(3)} disabled={countdown !== null}
          style={{ minWidth: 200 }}>
          {countdown !== null ? `${countdown}s 后批准...` : '批准，开始代码生成'}
        </Button>
      </div>
    </Modal>
  )
}

/** 架构生成阶段涉及的子步骤 ID（用于过滤 outputDetails） */
const ARCH_STEP_IDS = ARCH_STEPS.map(s => s.id)

export default function Phase1ArchBuild() {
  const stepStatus    = useRunStore(s => s.stepStatus)
  const stepOutputs   = useRunStore(s => s.stepOutputs)
  const archRetries   = useRunStore(s => s.archRetries)
  const outputDetails = useRunStore(s => s.outputDetails)

  const getStatus = (id: string): StepStatus => stepStatus[id] ?? 'pending'

  const validation = stepOutputs['validation'] as Record<string, unknown> | undefined
  const metricName = validation?.metric_name as string | undefined
  const direction  = validation?.direction  as string | undefined

  const backfill = stepOutputs['backfill'] as Record<string, unknown> | undefined
  const tree = backfill?.tree as Record<string, unknown>[] | undefined

  // 静态检查错误/警告详情
  const staticCheck = stepOutputs['static_check'] as Record<string, unknown> | undefined
  const staticErrors   = (staticCheck?.errors   as string[]) ?? []
  const staticWarnings = (staticCheck?.warnings as string[]) ?? []

  return (
    <>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <h3 className="text-primary mb-4">架构生成进度</h3>
          <div className="flex flex-col gap-1.5">
            {ARCH_STEPS.map(step => (
              <StepRow key={step.id} status={getStatus(step.id)} label={step.label}
                extra={stepExtra(step.id, getStatus(step.id), stepOutputs, archRetries, step.isMap)} />
            ))}
          </div>

          {/* 静态检查错误/警告详情 */}
          {staticErrors.length > 0 && (
            <div className="mt-4">
              <div className="text-danger text-xs font-semibold mb-2">静态检查错误 ({staticErrors.length})</div>
              <div className="flex flex-col gap-1 max-h-[150px] overflow-y-auto">
                {staticErrors.map((e, i) => (
                  <div key={i} className="px-3 py-1.5 bg-danger/10 rounded text-[11px] text-danger font-mono">{e}</div>
                ))}
              </div>
            </div>
          )}
          {staticWarnings.length > 0 && (
            <div className="mt-3">
              <div className="text-amber text-xs font-semibold mb-2">静态检查警告 ({staticWarnings.length})</div>
              <div className="flex flex-col gap-1 max-h-[100px] overflow-y-auto">
                {staticWarnings.map((w, i) => (
                  <div key={i} className="px-3 py-1.5 bg-amber/10 rounded text-[11px] text-amber font-mono">{w}</div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div>
          <h3 className="text-primary mb-4">项目树预览</h3>
          {metricName && (
            <div className="mb-3 flex gap-2">
              <Badge color="teal">指标: {metricName}</Badge>
              <Badge color="blue">方向: {direction === 'maximize' ? 'maximize \u2191' : 'minimize \u2193'}</Badge>
            </div>
          )}
          <div className="bg-surface rounded-lg p-4 font-mono text-xs min-h-[300px] leading-relaxed">
            {tree ? renderTree(tree) : (
              <div className="text-muted">
                <div className="mb-2">等待架构生成...</div>
                {['\uD83D\uDCC1 src/', '  \uD83D\uDCC4 ...', '\uD83D\uDCC4 main.py'].map((line, i) => (
                  <div key={i} className="opacity-30">{line}</div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* LLM / 工具输出详情 */}
      <OutputDetailPanel
        details={outputDetails}
        filterStepIds={ARCH_STEP_IDS}
        title="架构生成 LLM / 工具输出"
      />
    </>
  )
}
