import { Check, Circle, Bell } from 'lucide-react'
import { useRunStore } from '@/store/runStore'
import { PHASE_LABELS } from '@/types'
import type { Phase } from '@/types'
import { clsx } from 'clsx'

const ALL_PHASES: Phase[] = [0, 1, 2, 3, 4, 5, 6, 7]

function fmt(v: number | undefined): string {
  return v == null ? '\u2014' : v.toFixed(3)
}
function improvement(value: number | undefined, baseline: number | undefined): string | null {
  if (value == null || baseline == null || baseline === 0) return null
  const pct = ((value - baseline) / Math.abs(baseline)) * 100
  return pct > 0 ? `+${pct.toFixed(1)}%` : null
}

interface SideNavProps {
  open: boolean
  onClose: () => void
}

export default function SideNav({ open, onClose }: SideNavProps) {
  const phase = useRunStore(s => s.phase)
  const gateState = useRunStore(s => s.gateState)
  const metrics = useRunStore(s => s.metrics)
  const mctsGeneration = useRunStore(s => s.mctsGeneration)
  const u2eIteration = useRunStore(s => s.u2eIteration)
  const u2eFuncEvals = useRunStore(s => s.u2eFuncEvals)
  const u2eMaxFe = useRunStore(s => s.u2eMaxFe)

  const gatePhase: Phase | null =
    gateState?.gate_type === 'arch' ? 2 : gateState?.gate_type === 'mcts' ? 4 : null

  return (
    <>
      {/* Mobile overlay */}
      {open && (
        <div className="fixed inset-0 bg-black/50 z-30 md:hidden" onClick={onClose} />
      )}

      <nav
        className={clsx(
          'fixed top-12 left-0 w-[220px] h-[calc(100vh-48px)] bg-surface border-r border-subtle flex flex-col overflow-y-auto z-40 transition-transform duration-200',
          open ? 'translate-x-0' : '-translate-x-full md:translate-x-0',
        )}
        aria-label="Phase navigation"
      >
        {/* Phase list */}
        <div className="flex-1 py-2">
          {ALL_PHASES.map(p => {
            const isCompleted = p < phase
            const isCurrent = p === phase
            const isGateWaiting = gatePhase === p

            return (
              <div
                key={p}
                className={clsx(
                  'flex items-center gap-2.5 px-4 py-2.5 border-l-[3px] transition-colors cursor-default',
                  isCurrent && !isGateWaiting && 'border-l-blue bg-elevated',
                  isGateWaiting && 'border-l-amber bg-elevated',
                  !isCurrent && !isGateWaiting && 'border-l-transparent',
                )}
              >
                <div className="shrink-0 w-[18px] h-[18px] flex items-center justify-center">
                  {isGateWaiting ? (
                    <Bell size={15} className="animate-pulse-ring text-amber" />
                  ) : isCompleted ? (
                    <Check size={15} className="text-teal" strokeWidth={2.5} />
                  ) : isCurrent ? (
                    <div className="w-2 h-2 rounded-full bg-blue animate-pulse-ring" />
                  ) : (
                    <Circle size={8} className="text-muted" />
                  )}
                </div>
                <span className={clsx(
                  'text-[13px] truncate',
                  isCompleted && 'text-secondary',
                  (isCurrent || isGateWaiting) && 'text-primary font-semibold',
                  !isCompleted && !isCurrent && !isGateWaiting && 'text-muted',
                )}>
                  {PHASE_LABELS[p]}
                </span>
              </div>
            )
          })}
        </div>

        {/* Metrics panel */}
        <div className="border-t border-subtle px-4 py-3.5 flex flex-col gap-2.5">
          <span className="text-[11px] font-semibold text-muted uppercase tracking-wider">
            指标
          </span>
          <MetricRow label="基线" value={fmt(metrics.baseline)} />
          <MetricRow label="搜索最优" value={fmt(metrics.mcts_best)}
            prefix={metrics.mcts_best != null ? '\u2191 ' : ''}
            imp={improvement(metrics.mcts_best, metrics.baseline)} />
          <MetricRow label="进化最优" value={fmt(metrics.u2e_best)}
            prefix={metrics.u2e_best != null ? '\u2191\u2191 ' : ''}
            imp={improvement(metrics.u2e_best, metrics.baseline)} />

          {/* 搜索/进化进度指标 */}
          {(mctsGeneration > 0 || u2eIteration > 0) && (
            <>
              <span className="text-[11px] font-semibold text-muted uppercase tracking-wider mt-1">
                进度
              </span>
              {mctsGeneration > 0 && (
                <MetricRow label="搜索代数" value={String(mctsGeneration)} />
              )}
              {u2eIteration > 0 && (
                <MetricRow label="进化迭代" value={String(u2eIteration)} />
              )}
              {u2eFuncEvals > 0 && (
                <MetricRow label="函数评估" value={`${u2eFuncEvals} / ${u2eMaxFe}`} />
              )}
            </>
          )}
        </div>
      </nav>
    </>
  )
}

function MetricRow({ label, value, prefix, imp }: {
  label: string; value: string; prefix?: string; imp?: string | null
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[11px] text-muted">{label}</span>
      <div className="flex items-baseline gap-1.5">
        <span className="text-sm font-semibold text-primary font-mono">
          {prefix}{value}
        </span>
        {imp && <span className="text-[11px] text-teal font-semibold">{imp}</span>}
      </div>
    </div>
  )
}
