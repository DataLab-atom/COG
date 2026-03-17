import { useState } from 'react'
import { useRunStore } from '@/store/runStore'
import type { U2eIterProgress, StepStatus } from '@/types'
import { U2E_BODY_SUB_STEPS } from '@/types'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import { Card, StatCard, StepIcon } from '@/components/ui'
import { clsx } from 'clsx'

/* ── 迭代子步骤流水线（来自子图透传事件） ──────────────────────────────────────── */

const U2E_STEP_LABELS: Record<string, string> = {
  build_func_iter_tasks: '构建任务',
  func_evolve:           '并行进化',
  collect_results:       '结果汇总',
  update_state:          '状态更新',
}

function IterPipeline({ progress, loopIter }: { progress: U2eIterProgress; loopIter: number }) {
  const hasAny = U2E_BODY_SUB_STEPS.some(s => progress[s] !== 'pending')
  if (!hasAny && loopIter === 0) return null

  return (
    <Card className="mb-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="m-0 text-sm text-primary">
          当前迭代流水线
        </h3>
        {loopIter > 0 && (
          <span className="text-[11px] text-muted font-mono">loop #{loopIter}</span>
        )}
      </div>
      <div className="flex items-center gap-1.5">
        {U2E_BODY_SUB_STEPS.map((step, i) => {
          const status: StepStatus = progress[step]
          return (
            <div key={step} className="contents">
              <div className={clsx(
                'flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] transition-all',
                status === 'running' ? 'bg-blue/15 text-blue font-semibold' :
                status === 'done'    ? 'bg-teal/10 text-teal' :
                status === 'error'   ? 'bg-danger/10 text-danger' :
                'bg-elevated text-muted'
              )}>
                <StepIcon status={status} size={12} />
                {U2E_STEP_LABELS[step]}
              </div>
              {i < U2E_BODY_SUB_STEPS.length - 1 && (
                <span className="text-strong text-[10px]">&rarr;</span>
              )}
            </div>
          )
        })}
      </div>
    </Card>
  )
}

export default function Phase5U2e() {
  const u2eIteration   = useRunStore(s => s.u2eIteration)
  const u2eFuncEvals   = useRunStore(s => s.u2eFuncEvals)
  const u2eMaxFe       = useRunStore(s => s.u2eMaxFe)
  const u2eHistory     = useRunStore(s => s.u2eHistory)
  const metrics        = useRunStore(s => s.metrics)
  const stepOutputs    = useRunStore(s => s.stepOutputs)
  const u2eIterProgress = useRunStore(s => s.u2eIterProgress)
  const u2eLoopIter    = useRunStore(s => s.u2eLoopIter)
  const [expandedFunc, setExpandedFunc] = useState<string | null>(null)

  const funcNames = ((stepOutputs['tree_to_func_tasks'] as Record<string, unknown> | undefined)?.func_names as string[]) ?? []

  const baseline    = metrics.baseline ?? 0
  const u2eBest     = metrics.u2e_best ?? metrics.mcts_best ?? baseline
  const improvement = baseline > 0 ? ((u2eBest - baseline) / Math.abs(baseline) * 100) : 0
  const feProgress  = u2eMaxFe > 0 ? (u2eFuncEvals / u2eMaxFe * 100) : 0

  // 真实历史数据（从 store 累积）
  const historyData = u2eHistory.map(pt => ({
    fe: pt.function_evals,
    best: Number(pt.best_obj.toFixed(4)),
  }))

  return (
    <div className="flex flex-col gap-4">
      {/* Top stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard label="迭代轮次" value={`${u2eIteration} / 200`} />
        <Card className="flex-1">
          <div className="text-xs text-muted mb-1.5">函数评估次数</div>
          <div className="text-xl font-bold font-mono">{u2eFuncEvals.toLocaleString()} / {u2eMaxFe.toLocaleString()}</div>
          <div className="mt-2 h-1 bg-elevated rounded-sm">
            <div className="h-full bg-blue rounded-sm transition-all duration-500" style={{ width: `${feProgress}%` }} />
          </div>
        </Card>
        <StatCard label="当前最优"
          value={u2eBest > 0 ? u2eBest.toFixed(4) : '\u2014'}
          sub={improvement > 0 ? `+${improvement.toFixed(1)}% vs 基线` : undefined} />
        <StatCard label="基线" value={baseline > 0 ? baseline.toFixed(4) : '\u2014'} />
      </div>

      {/* 子图透传：迭代子步骤流水线 */}
      <IterPipeline progress={u2eIterProgress} loopIter={u2eLoopIter} />

      {/* Middle: function list + evolution curve */}
      <div className="grid grid-cols-1 lg:grid-cols-[55%_45%] gap-4">
        {/* Left: function status */}
        <Card>
          <h3 className="m-0 mb-4 text-sm">各函数进化状态</h3>
          {funcNames.length === 0 ? (
            <div className="text-muted text-[13px] py-8 text-center">等待函数任务初始化...</div>
          ) : (
            <div className="overflow-x-auto">
              {funcNames.map(fname => {
                const isExpanded = expandedFunc === fname
                return (
                  <div key={fname} className="mb-2">
                    <button
                      className="flex items-center gap-2.5 w-full text-left cursor-pointer bg-transparent border-none p-1"
                      onClick={() => setExpandedFunc(isExpanded ? null : fname)}>
                      <span className="text-xs text-secondary w-40 shrink-0 truncate font-mono">{fname}</span>
                      <div className="flex-1 h-2 bg-elevated rounded-sm overflow-hidden">
                        <div className="h-full bg-teal/40 rounded-sm transition-all duration-300"
                          style={{ width: `${Math.min(100, (u2eIteration / 200) * 100)}%` }} />
                      </div>
                      <span className="text-[10px] text-muted">{isExpanded ? '\u25B2' : '\u25B6'}</span>
                    </button>
                    {isExpanded && (
                      <div className="mt-2 ml-[170px] bg-elevated rounded-md p-2.5">
                        <div className="text-[11px] text-muted mb-1">进化进度: 第 {u2eIteration} 轮</div>
                        <div className="text-[11px] text-secondary">
                          当前全局最优: {u2eBest > 0 ? u2eBest.toFixed(4) : '计算中...'}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </Card>

        {/* Right: evolution curve */}
        <Card>
          <h3 className="m-0 mb-4 text-sm">目标值进化曲线</h3>
          {historyData.length > 0 ? (
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={historyData} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                <XAxis dataKey="fe" stroke="var(--color-muted)" tick={{ fontSize: 10 }}
                  tickFormatter={v => v > 999 ? `${(v/1000).toFixed(1)}k` : String(v)} />
                <YAxis stroke="var(--color-muted)" tick={{ fontSize: 10 }} domain={['auto', 'auto']} />
                <Tooltip contentStyle={{ background: 'var(--color-overlay)', border: '1px solid var(--color-strong)', borderRadius: 6, fontSize: 12 }} />
                {baseline > 0 && <ReferenceLine y={baseline} stroke="var(--color-danger)" strokeDasharray="4 2"
                  label={{ value: '基线', fill: 'var(--color-danger)', fontSize: 10 }} />}
                <Line type="monotone" dataKey="best" stroke="var(--color-teal)" strokeWidth={2} dot={false} name="全局最优" />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[220px] flex items-center justify-center text-muted text-[13px]">
              {u2eIteration > 0 ? '收集数据中...' : '等待进化开始...'}
            </div>
          )}

          {/* Stats table */}
          <div className="mt-3">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="border-b border-subtle">
                  <th className="text-left px-2 py-1 text-muted font-normal">指标</th>
                  <th className="text-right px-2 py-1 text-muted font-normal">值</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ['累计迭代', String(u2eIteration)],
                  ['累计函数评估', u2eFuncEvals.toLocaleString()],
                  ['历史数据点', String(u2eHistory.length)],
                ].map(([label, val]) => (
                  <tr key={label}>
                    <td className="px-2 py-1 text-secondary">{label}</td>
                    <td className="px-2 py-1 text-right font-mono">{val}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  )
}
