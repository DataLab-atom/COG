import { useRunStore } from '@/store/runStore'
import type { StepStatus } from '@/types'
import { Card, Badge, Button, ProgressRing } from '@/components/ui'
import { clsx } from 'clsx'

const PAPER_STEPS = [
  { id: 'build_method_explain', label: '方法提取' },
  { id: 'build_references',     label: '文献下载' },
  { id: 'build_database',       label: '向量数据库' },
  { id: 'experimenting',        label: '实验分析' },
]

const CHAPTER_STEPS = [
  { id: 'method_writing',       label: 'Method',       zh: '方法',   deps: ['build_method_explain'] },
  { id: 'related_work_writing', label: 'Related Work', zh: '相关工作', deps: ['build_database'] },
  { id: 'experiment_writing',   label: 'Experiment',   zh: '实验',   deps: ['experimenting'] },
  { id: 'introduction_writing', label: 'Introduction', zh: '引言',   deps: ['method_writing', 'related_work_writing', 'experiment_writing'] },
]

function PipelinePill({ label, status, sub }: { label: string; status: StepStatus; sub?: string }) {
  const colorMap: Record<StepStatus, string> = { done: 'teal', running: 'blue', error: 'danger', pending: 'muted' }
  return (
    <Badge color={colorMap[status]}>
      {status === 'running' && <span className="animate-spin inline-block text-xs">{'\u2699'}</span>}
      {status === 'done' && '\u2713 '}
      {label}
      {sub && <span className="text-muted ml-1">{sub}</span>}
    </Badge>
  )
}

function ChapterCard({ id, label, zh, status, deps, depStatus, stepOutputs }: {
  id: string; label: string; zh: string; status: StepStatus; deps: string[]
  depStatus: (id: string) => StepStatus; stepOutputs: Record<string, unknown>
}) {
  const locked = deps.some(d => depStatus(d) !== 'done')
  const pct = status === 'done' ? 100 : status === 'running' ? 50 : 0

  return (
    <Card highlight={status === 'running' ? 'blue' : status === 'done' ? 'teal' : null} className="relative overflow-hidden">
      {locked && status === 'pending' && (
        <div className="absolute inset-0 bg-base/60 flex items-center justify-center rounded-lg z-10">
          <span className="text-3xl" aria-label="locked">{'\uD83D\uDD12'}</span>
        </div>
      )}
      <div className="flex justify-between items-start mb-3">
        <div>
          <div className="font-bold mb-0.5">{zh}</div>
          <div className="text-xs text-muted italic">{label}</div>
        </div>
        <ProgressRing pct={pct} size={50} />
      </div>
      {status === 'running' && (
        <div className="text-xs text-blue flex items-center gap-1.5">
          <span className="animate-spin inline-block">{'\u2699'}</span> 写作中...
        </div>
      )}
      {status === 'done' && (
        <div className="text-xs text-teal">{'\u2713'} 已完成</div>
      )}
      {status === 'pending' && !locked && (
        <div className="text-xs text-muted">等待中...</div>
      )}
    </Card>
  )
}

export default function Phase6Paper() {
  const stepStatus  = useRunStore(s => s.stepStatus)
  const stepOutputs = useRunStore(s => s.stepOutputs)
  const paperSteps  = useRunStore(s => s.paperSteps)

  const getStatus = (id: string): StepStatus => (stepStatus[id] ?? paperSteps[id]) as StepStatus ?? 'pending'

  const refOutputs   = stepOutputs['build_references'] as Record<string, unknown> | undefined
  const mergeOutputs = stepOutputs['merge_writing']   as Record<string, unknown> | undefined
  const mergeStatus  = getStatus('merge_writing')
  const paperTexPath = mergeOutputs?.paper_tex_path as string | undefined

  return (
    <div className="flex flex-col gap-6">
      {/* Pipeline flow */}
      <div>
        <h3 className="m-0 mb-4 text-sm text-primary">文献准备</h3>
        <div className="flex items-center gap-2 flex-wrap">
          {PAPER_STEPS.map((step, i) => (
            <div key={step.id} className="contents">
              <PipelinePill label={step.label} status={getStatus(step.id)}
                sub={step.id === 'build_references' && refOutputs ? `${(refOutputs.paper_count as number) ?? '?'} 篇` : undefined} />
              {i < PAPER_STEPS.length - 1 && <span className="text-strong">{'\u2192\u2192'}</span>}
            </div>
          ))}
        </div>
      </div>

      {/* 4 chapters */}
      <div>
        <h3 className="m-0 mb-4 text-sm text-primary">四章节并行写作</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {CHAPTER_STEPS.map(step => (
            <ChapterCard key={step.id} {...step} status={getStatus(step.id)} depStatus={getStatus} stepOutputs={stepOutputs} />
          ))}
        </div>
      </div>

      {/* Merge output */}
      <Card highlight={mergeStatus === 'done' ? 'teal' : mergeStatus === 'running' ? 'blue' : null}>
        <div className="flex items-center justify-between flex-wrap gap-4">
          <div>
            <div className="font-bold mb-1">合并输出 {'\u2192'} paper.tex</div>
            {mergeStatus === 'pending' && <div className="text-xs text-muted">等待四章全部完成...</div>}
            {mergeStatus === 'running' && (
              <div className="text-xs text-blue flex items-center gap-1.5">
                <span className="animate-spin inline-block">{'\u2699'}</span>整合并生成 paper.tex...
              </div>
            )}
            {mergeStatus === 'done' && (
              <div>
                <div className="text-sm text-teal font-semibold">paper.tex 已生成</div>
                {paperTexPath && <div className="text-xs text-muted mt-1 font-mono">{paperTexPath}</div>}
              </div>
            )}
          </div>
          {mergeStatus === 'done' && paperTexPath && (
            <a href={`/api/files?path=${encodeURIComponent(paperTexPath)}`} download
              className="inline-flex items-center gap-1 px-4 py-2 border-none rounded-md bg-teal text-base font-semibold cursor-pointer no-underline text-sm">
              {'\u2B07'} 下载
            </a>
          )}
        </div>
      </Card>
    </div>
  )
}
