import { useState, useRef, useEffect, useMemo } from 'react'
import { clsx } from 'clsx'
import type { StepOutputDetail } from '@/types'

/** 格式化输出内容：对象显示为缩进 JSON，字符串直接显示 */
function formatOutput(output: unknown): string {
  if (output == null) return '(empty)'
  if (typeof output === 'string') return output
  try {
    return JSON.stringify(output, null, 2)
  } catch {
    return String(output)
  }
}

/** 从 ref 路径提取简短名称 */
function shortRef(ref: string): string {
  const parts = ref.split('/')
  return parts[parts.length - 1].replace(/\.json$/, '')
}

/** 单条记录（展开后显示 params + output） */
function DetailItem({ detail }: { detail: StepOutputDetail }) {
  const [open, setOpen] = useState(false)
  const text = formatOutput(detail.output)
  const isLong = text.length > 300

  return (
    <div className="border border-subtle rounded-md overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className={clsx(
          'w-full text-left px-3 py-1.5 flex items-center gap-2 border-none cursor-pointer text-[11px]',
          detail.type === 'llm' ? 'bg-blue/8' : 'bg-teal/8',
        )}
      >
        <span className={clsx(
          'shrink-0 px-1.5 py-0.5 rounded text-[10px] font-bold uppercase',
          detail.type === 'llm' ? 'bg-blue/20 text-blue' : 'bg-teal/20 text-teal',
        )}>
          {detail.type}
        </span>
        <span className="text-muted font-mono text-[10px]">{shortRef(detail.ref)}</span>
        <span className="ml-auto text-muted text-[10px]">
          {new Date(detail.ts).toLocaleTimeString()}
        </span>
        <span className="text-muted text-[10px]">{open ? '\u25B2' : '\u25BC'}</span>
      </button>
      {open && (
        <div className="px-3 py-2 bg-elevated">
          {detail.params && (
            <div className="mb-2">
              <div className="text-[10px] text-muted mb-1 uppercase font-bold">params</div>
              <pre className="text-[11px] text-secondary font-mono whitespace-pre-wrap break-all m-0 max-h-[100px] overflow-y-auto">
                {formatOutput(detail.params)}
              </pre>
            </div>
          )}
          <div>
            <div className="text-[10px] text-muted mb-1 uppercase font-bold">output</div>
            <pre className={clsx(
              'text-[11px] text-primary font-mono whitespace-pre-wrap break-all m-0 overflow-y-auto',
              isLong ? 'max-h-[300px]' : '',
            )}>
              {text}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}

/** 按 stepId 分组的折叠区域 */
function StepGroup({ stepId, items, defaultOpen }: {
  stepId: string; items: StepOutputDetail[]; defaultOpen: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const llmCount = items.filter(d => d.type === 'llm').length
  const toolCount = items.filter(d => d.type === 'tool').length

  return (
    <div className="border border-subtle rounded-md overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full text-left px-3 py-2 flex items-center gap-2 border-none cursor-pointer bg-elevated text-[12px]"
      >
        <span className="text-primary font-mono font-semibold">{stepId}</span>
        <span className="flex gap-1">
          {llmCount > 0 && (
            <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-blue/20 text-blue">
              LLM {llmCount > 1 ? `\u00D7${llmCount}` : ''}
            </span>
          )}
          {toolCount > 0 && (
            <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-teal/20 text-teal">
              TOOL {toolCount > 1 ? `\u00D7${toolCount}` : ''}
            </span>
          )}
        </span>
        <span className="ml-auto text-muted text-[10px]">
          {new Date(items[0].ts).toLocaleTimeString()}
          {items.length > 1 && ` \u2014 ${new Date(items[items.length - 1].ts).toLocaleTimeString()}`}
        </span>
        <span className="text-muted text-[10px]">{open ? '\u25B2' : '\u25BC'}</span>
      </button>
      {open && (
        <div className="p-2 bg-surface flex flex-col gap-1.5">
          {items.map((d, i) => (
            <DetailItem key={`${d.stepId}-${d.ts}-${i}`} detail={d} />
          ))}
        </div>
      )}
    </div>
  )
}

type TypeFilter = 'all' | 'llm' | 'tool'

interface OutputDetailPanelProps {
  /** 过滤的步骤 ID 列表；为空数组则显示全部 */
  filterStepIds?: string[]
  details: StepOutputDetail[]
  title?: string
}

export default function OutputDetailPanel({ filterStepIds, details, title = 'LLM / 工具输出详情' }: OutputDetailPanelProps) {
  const [open, setOpen] = useState(false)
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all')
  const [expandAll, setExpandAll] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  const filtered = useMemo(() => {
    let list = filterStepIds && filterStepIds.length > 0
      ? details.filter(d => filterStepIds.includes(d.stepId))
      : details
    if (typeFilter !== 'all') {
      list = list.filter(d => d.type === typeFilter)
    }
    return list
  }, [details, filterStepIds, typeFilter])

  // 按 stepId 分组（保持出现顺序）
  const groups = useMemo(() => {
    const map = new Map<string, StepOutputDetail[]>()
    for (const d of filtered) {
      const arr = map.get(d.stepId)
      if (arr) arr.push(d)
      else map.set(d.stepId, [d])
    }
    return Array.from(map.entries())
  }, [filtered])

  const llmTotal = useMemo(() => filtered.filter(d => d.type === 'llm').length, [filtered])
  const toolTotal = useMemo(() => filtered.filter(d => d.type === 'tool').length, [filtered])

  // 新增内容时自动滚动到底部
  useEffect(() => {
    if (open && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [filtered.length, open])

  if (details.length === 0) return null
  const totalFiltered = filtered.length
  const totalAll = filterStepIds && filterStepIds.length > 0
    ? details.filter(d => filterStepIds.includes(d.stepId)).length
    : details.length

  return (
    <div className="mt-4 border border-subtle rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full text-left px-4 py-2.5 bg-elevated border-none text-primary font-semibold cursor-pointer flex justify-between items-center text-[13px]"
        aria-expanded={open}
      >
        <span>{title} ({totalAll})</span>
        <span className="text-muted">{open ? '\u25B2' : '\u25BC'}</span>
      </button>
      {open && (
        <div className="bg-surface">
          {/* 工具栏：类型筛选 + 展开/收起全部 */}
          <div className="px-3 pt-2 pb-1 flex items-center gap-2 flex-wrap">
            {(['all', 'llm', 'tool'] as const).map(f => (
              <button
                key={f}
                onClick={() => setTypeFilter(f)}
                className={clsx(
                  'px-2 py-0.5 rounded text-[11px] font-bold uppercase border-none cursor-pointer transition-all',
                  typeFilter === f
                    ? f === 'llm' ? 'bg-blue/25 text-blue'
                      : f === 'tool' ? 'bg-teal/25 text-teal'
                      : 'bg-strong text-primary'
                    : 'bg-elevated text-muted',
                )}
              >
                {f === 'all' ? `全部 ${totalAll}` : f === 'llm' ? `LLM ${llmTotal}` : `TOOL ${toolTotal}`}
              </button>
            ))}
            <div className="ml-auto flex gap-1">
              <button
                onClick={() => setExpandAll(true)}
                className="px-2 py-0.5 rounded text-[10px] bg-elevated text-muted border-none cursor-pointer"
                title="展开全部分组"
              >
                全部展开
              </button>
              <button
                onClick={() => setExpandAll(false)}
                className="px-2 py-0.5 rounded text-[10px] bg-elevated text-muted border-none cursor-pointer"
                title="收起全部分组"
              >
                全部收起
              </button>
            </div>
          </div>
          {/* 分组列表 */}
          <div className="p-3 pt-1 flex flex-col gap-2 max-h-[600px] overflow-y-auto">
            {groups.length === 0 && (
              <div className="text-muted text-[12px] text-center py-3">无匹配记录</div>
            )}
            {groups.map(([stepId, items], i) => (
              <StepGroup
                key={`${stepId}-${i}`}
                stepId={stepId}
                items={items}
                defaultOpen={expandAll || i === groups.length - 1}
              />
            ))}
            <div ref={bottomRef} />
          </div>
        </div>
      )}
    </div>
  )
}
