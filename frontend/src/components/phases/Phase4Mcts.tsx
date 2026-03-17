import { useState } from 'react'
import { useRunStore } from '@/store/runStore'
import type { MctsNode, MctsAction, NextMode, MctsEvalProgress } from '@/types'
import { MCTS_COMBO_SUB_STEPS } from '@/types'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import { Card, Button, Badge } from '@/components/ui'
import OutputDetailPanel from '@/components/OutputDetailPanel'
import { clsx } from 'clsx'

/* ── 搜索树可视化（带父子路径的真实树形结构）───────────────────────────────── */

/** 构建以 node_id 为 key 的子节点映射 */
function buildChildrenMap(nodes: Record<string, MctsNode>): Record<string, string[]> {
  const children: Record<string, string[]> = {}
  for (const n of Object.values(nodes)) {
    if (n.parent_id && nodes[n.parent_id]) {
      if (!children[n.parent_id]) children[n.parent_id] = []
      children[n.parent_id].push(n.node_id)
    }
  }
  for (const kids of Object.values(children)) {
    kids.sort((a, b) => (nodes[b]?.score ?? 0) - (nodes[a]?.score ?? 0))
  }
  return children
}

/** 找到从某节点到根的完整路径 */
function pathToRoot(nodeId: string, nodes: Record<string, MctsNode>): string[] {
  const path: string[] = []
  let cur: string | null = nodeId
  const visited = new Set<string>()
  while (cur && nodes[cur] && !visited.has(cur)) {
    visited.add(cur)
    path.unshift(cur)
    cur = nodes[cur].parent_id
  }
  return path
}

function getNodeColor(n: MctsNode, bestId: string | null, frontier: string[]) {
  if (n.score < -100)               return 'var(--color-danger)'
  if (n.node_id === bestId)         return 'var(--color-teal)'
  if (frontier.includes(n.node_id)) return 'var(--color-blue)'
  return 'var(--color-purple)'
}

/** 递归渲染树节点及其子树 */
function TreeNode({ nodeId, nodes, childrenMap, frontier, bestId, selected, onSelect }: {
  nodeId: string; nodes: Record<string, MctsNode>; childrenMap: Record<string, string[]>
  frontier: string[]; bestId: string | null; selected: string | null
  onSelect: (id: string) => void
}) {
  const n = nodes[nodeId]
  if (!n) return null
  const kids = childrenMap[nodeId] ?? []
  const color = getNodeColor(n, bestId, frontier)
  const isSelected = selected === nodeId
  const isBest = nodeId === bestId

  return (
    <div className="flex flex-col items-center">
      {/* 节点卡片 */}
      <button
        onClick={() => onSelect(nodeId)}
        className={clsx(
          'w-[68px] p-1.5 rounded-md cursor-pointer text-center border-none transition-all',
          isSelected ? 'bg-overlay' : 'bg-elevated',
        )}
        style={{
          outline: `2px solid ${color}`,
          boxShadow: isBest ? '0 0 10px rgba(52,200,160,0.4)' : isSelected ? `0 0 8px ${color}40` : undefined,
        }}
      >
        <div className="text-[9px] text-muted leading-none mb-0.5 font-mono">
          {n.node_id === 'root' ? 'root' : n.node_id.slice(0, 6)}
        </div>
        <div className="text-[12px] font-bold font-mono leading-tight" style={{ color }}>
          {n.score < -100 ? 'FAIL' : n.score.toFixed(3)}
        </div>
        {n.op && <div className="text-[9px] text-muted leading-none mt-0.5 truncate">{n.op.slice(0, 10)}</div>}
        {isBest && <div className="text-[9px] leading-none">{'\u2605'}</div>}
      </button>

      {/* 子节点连线与递归渲染 */}
      {kids.length > 0 && (
        <>
          {/* 父节点到横杆的竖线 */}
          <div className="w-px h-3" style={{ background: 'var(--color-subtle)' }} />
          {/* 子节点容器 */}
          <div className="flex gap-1 items-start relative">
            {/* 横杆（仅多子节点时显示） */}
            {kids.length > 1 && (
              <div className="absolute top-0 h-px" style={{
                background: 'var(--color-subtle)',
                left: '50%', right: '50%',
                // 从第一个子节点中心到最后一个子节点中心
                marginLeft: `-${(kids.length - 1) * 36}px`,
                marginRight: `-${(kids.length - 1) * 36}px`,
                width: `${(kids.length - 1) * 72}px`,
                transform: 'translateX(-50%)',
              }} />
            )}
            {kids.map(kidId => (
              <div key={kidId} className="flex flex-col items-center">
                {/* 横杆到子节点的竖线 */}
                <div className="w-px h-3" style={{ background: 'var(--color-subtle)' }} />
                <TreeNode
                  nodeId={kidId} nodes={nodes} childrenMap={childrenMap}
                  frontier={frontier} bestId={bestId} selected={selected}
                  onSelect={onSelect}
                />
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function MctsTreeViz({ nodes, frontier, bestNode }: {
  nodes: Record<string, MctsNode>; frontier: string[]; bestNode: MctsNode | null
}) {
  const [selected, setSelected] = useState<string | null>(null)

  const childrenMap = buildChildrenMap(nodes)
  const bestId = bestNode?.node_id ?? null
  const selectedNode = selected ? nodes[selected] : null

  // 找到根节点
  const roots = Object.values(nodes).filter(n => !n.parent_id || !nodes[n.parent_id])
  roots.sort((a, b) => a.generation - b.generation)

  // 选中节点到根的路径
  const highlightPath = selected ? pathToRoot(selected, nodes) : []

  const toggleSelect = (id: string) => setSelected(s => s === id ? null : id)

  return (
    <div>
      {/* 图例 */}
      <div className="flex gap-3 mb-3 flex-wrap">
        {([['var(--color-blue)', 'frontier'], ['var(--color-teal)', '最优'], ['var(--color-purple)', '历史'], ['var(--color-danger)', '失败']] as const).map(([color, label]) => (
          <div key={label} className="flex items-center gap-1">
            <div className="w-2.5 h-2.5 rounded-sm" style={{ background: color }} />
            <span className="text-[11px] text-muted">{label}</span>
          </div>
        ))}
      </div>

      {/* 树形结构（可双向滚动） */}
      <div className="overflow-auto max-h-[350px]">
        <div className="inline-flex flex-col items-center min-w-full pb-2">
          {roots.map(root => (
            <TreeNode
              key={root.node_id} nodeId={root.node_id} nodes={nodes}
              childrenMap={childrenMap} frontier={frontier} bestId={bestId}
              selected={selected} onSelect={toggleSelect}
            />
          ))}
          {roots.length === 0 && <div className="text-muted text-[13px] py-4">搜索树初始化中...</div>}
        </div>
      </div>

      {/* 选中节点详情 */}
      {selectedNode && (
        <div className="mt-3 bg-elevated rounded-md p-3 text-xs">
          <div className="flex items-center justify-between mb-1.5">
            <span className="font-semibold">{selectedNode.node_id}</span>
            <Badge color={selectedNode.score < -100 ? 'danger' : 'teal'}>
              {selectedNode.score < -100 ? 'FAIL' : selectedNode.score.toFixed(4)}
            </Badge>
          </div>
          <div className="text-secondary mb-1">
            op: <span className="text-primary">{selectedNode.op ?? '\u2014'}</span>
            {' / '}
            angle: <span className="text-primary">{selectedNode.angle ?? '\u2014'}</span>
          </div>
          {/* 到根的路径 */}
          {highlightPath.length > 1 && (
            <div className="text-[11px] text-muted mb-1.5">
              路径: {highlightPath.map(id => id === 'root' ? 'root' : id.slice(0, 6)).join(' \u2192 ')}
            </div>
          )}
          {selectedNode.output_log && (
            <pre className="m-0 text-[11px] text-muted max-h-24 overflow-auto whitespace-pre-wrap bg-base rounded p-2">
              {selectedNode.output_log.slice(0, 500)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

/* ── Gate 面板 ─────────────────────────────────────────────────────────────────── */

function GatePanel({ generation, nodes }: { generation: number; nodes: Record<string, MctsNode> }) {
  const gateState  = useRunStore(s => s.gateState)
  const metrics    = useRunStore(s => s.metrics)
  const submitGate = useRunStore(s => s.submitGate)
  const [action, setAction]        = useState<MctsAction>('continue')
  const [nextMode, setNextMode]    = useState<NextMode>('llm')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [confirmArch, setConfirmArch] = useState(false)

  const newNodes = (gateState?.new_nodes ?? []) as MctsNode[]
  const baseline = metrics.baseline ?? 0

  if (!gateState || gateState.gate_type !== 'mcts') {
    return (
      <Card>
        <div className="text-muted text-sm text-center py-4">
          <div className="animate-pulse-ring text-2xl mb-2">{'\u23F3'}</div>
          正在评估第 {generation} 代...
          <div className="mt-2 text-xs">Gate 面板在本代完成后解锁</div>
        </div>
      </Card>
    )
  }

  const ACTIONS: { key: MctsAction; label: string; color: string }[] = [
    { key: 'continue',     label: '\u25B6  继续搜索',         color: 'teal' },
    { key: 'select',       label: '\uD83C\uDFAF 手动选择 frontier', color: 'blue' },
    { key: 'rollback',     label: '\u23EA 回滚到上一代',       color: 'amber' },
    { key: 'architecture', label: '\uD83C\uDFDB 触发架构重设计',  color: 'danger' },
    { key: 'stop',         label: '\u23F9 停止并写回最优',     color: 'muted' },
  ]

  const handleSubmit = () => {
    if (action === 'architecture' && !confirmArch) { setConfirmArch(true); return }
    submitGate('mcts_gate', { action, next_mode: nextMode, selected_ids: selectedIds })
    setConfirmArch(false)
  }

  return (
    <Card highlight="amber">
      <div className="text-amber font-bold mb-4">第 {gateState.generation ?? generation} 代已完成</div>

      {newNodes.length > 0 && (
        <div className="mb-4">
          <div className="text-xs text-secondary mb-2">新增节点:</div>
          {newNodes.sort((a, b) => b.score - a.score).map((n, i) => {
            const imp = baseline > 0 ? ((n.score - baseline) / Math.abs(baseline) * 100) : 0
            return (
              <div key={n.node_id} className="flex justify-between items-center px-2.5 py-1.5 mb-1 bg-elevated rounded-md">
                <span className="font-mono text-xs text-muted">{n.node_id.slice(0, 8)}{i === 0 ? ' \u2605' : ''}</span>
                <span className="font-bold font-mono">{n.score.toFixed(3)}</span>
                <span className={clsx('text-xs', imp >= 0 ? 'text-teal' : 'text-danger')}>
                  {imp >= 0 ? '+' : ''}{imp.toFixed(1)}%
                </span>
              </div>
            )
          })}
        </div>
      )}

      <div className="flex flex-col gap-2">
        {ACTIONS.map(({ key, label, color }) => (
          <button key={key} onClick={() => { setAction(key); setConfirmArch(false) }}
            className={clsx(
              'px-3.5 py-2.5 rounded-md cursor-pointer text-left text-[13px] border transition-all',
              action === key ? `font-bold border-${color} bg-${color}/15` : 'font-normal border-subtle bg-transparent',
            )}
            style={{ color: `var(--color-${color})` }}>
            {label}
          </button>
        ))}
      </div>

      {action === 'select' && newNodes.length > 0 && (
        <div className="mt-3 p-3 bg-elevated rounded-md">
          <div className="text-xs text-secondary mb-2">选择要保留的节点:</div>
          {newNodes.map(n => (
            <label key={n.node_id} className="flex items-center gap-2 mb-1.5 cursor-pointer">
              <input type="checkbox" checked={selectedIds.includes(n.node_id)}
                onChange={e => setSelectedIds(ids => e.target.checked ? [...ids, n.node_id] : ids.filter(id => id !== n.node_id))}
                className="accent-blue" />
              <span className="font-mono text-xs">{n.node_id.slice(0, 8)} {'\u2014'} {n.score.toFixed(3)}</span>
            </label>
          ))}
        </div>
      )}

      {confirmArch && (
        <div className="mt-3 p-3 bg-danger/10 border border-danger rounded-md text-[13px]">
          确认将终止当前搜索并触发架构重设计？
          <div className="flex gap-2 mt-2">
            <Button variant="ghost" size="sm" className="flex-1" onClick={() => setConfirmArch(false)}>取消</Button>
            <Button variant="danger" size="sm" className="flex-1" onClick={handleSubmit}
              style={{ background: 'var(--color-danger)', color: '#fff' }}>确认重设计</Button>
          </div>
        </div>
      )}

      <div className="flex gap-2 mt-4 items-center">
        <select value={nextMode} onChange={e => setNextMode(e.target.value as NextMode)}
          className="bg-elevated border border-subtle text-primary rounded-md px-2.5 py-1.5 text-[13px] flex-1 outline-none">
          <option value="llm">next_mode: llm</option>
          <option value="random">next_mode: random</option>
        </select>
        <Button variant="primary" size="md" className="flex-[2]" onClick={handleSubmit} disabled={confirmArch}>
          提交决策
        </Button>
      </div>
    </Card>
  )
}

/* ── 分数历史图 ──────────────────────────────────────────────────────────────── */

function ScoreHistory({ nodes, baseline }: { nodes: Record<string, MctsNode>; baseline: number }) {
  const byGen: Record<number, number> = {}
  for (const n of Object.values(nodes)) {
    if (n.score > -100) byGen[n.generation] = Math.max(byGen[n.generation] ?? -Infinity, n.score)
  }
  const data = Object.entries(byGen).sort(([a], [b]) => Number(a) - Number(b)).map(([gen, score]) => ({
    gen: Number(gen), score: Number(score.toFixed(4)),
    global: Object.entries(byGen).filter(([g]) => Number(g) <= Number(gen)).reduce((m, [, s]) => Math.max(m, s), -Infinity),
  }))

  if (data.length === 0) return <div className="h-[180px] flex items-center justify-center text-muted text-[13px]">等待数据...</div>

  return (
    <ResponsiveContainer width="100%" height={180}>
      <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <XAxis dataKey="gen" stroke="var(--color-muted)" tick={{ fontSize: 11 }} />
        <YAxis stroke="var(--color-muted)" tick={{ fontSize: 11 }} domain={['auto', 'auto']} />
        <Tooltip contentStyle={{ background: 'var(--color-overlay)', border: '1px solid var(--color-strong)', borderRadius: 6, fontSize: 12 }} />
        {baseline > 0 && <ReferenceLine y={baseline} stroke="var(--color-muted)" strokeDasharray="4 2" label={{ value: '基线', fill: 'var(--color-muted)', fontSize: 11 }} />}
        <Line type="monotone" dataKey="score"  stroke="var(--color-blue)" strokeWidth={2} dot={{ r: 3, fill: 'var(--color-blue)' }} name="代最优" />
        <Line type="monotone" dataKey="global" stroke="var(--color-teal)" strokeWidth={2} dot={false} name="全局最优" />
      </LineChart>
    </ResponsiveContainer>
  )
}

/* ── Combo 评估流水线进度 ────────────────────────────────────────────────────── */

const COMBO_STEP_LABELS: Record<string, string> = {
  critic:           'Critic 分析',
  parse_atomic_ops: '任务拆解',
  engineer_tasks:   'Engineer 生成',
  collect_patches:  'Patch 收集',
  sandbox:          '沙盒评估',
}

function EvalPipeline({ progress }: { progress: MctsEvalProgress }) {
  const { started, done, beamCount } = progress
  const total = beamCount || 1
  const hasAny = MCTS_COMBO_SUB_STEPS.some(s => started[s] > 0)
  if (!hasAny) return null

  return (
    <div className="mt-3">
      <div className="text-[11px] text-muted mb-2">Beam 评估流水线 ({total} 路并行)</div>
      <div className="flex flex-col gap-1">
        {MCTS_COMBO_SUB_STEPS.map(step => {
          const s = started[step]
          const d = done[step]
          const pct = total > 0 ? Math.round((d / total) * 100) : 0
          const isActive = s > d
          const isDone = d >= total && total > 0
          return (
            <div key={step} className="flex items-center gap-2">
              <span className="text-[11px] w-24 shrink-0 text-secondary truncate">
                {COMBO_STEP_LABELS[step]}
              </span>
              <div className="flex-1 h-1.5 bg-elevated rounded-sm overflow-hidden">
                <div
                  className={clsx('h-full rounded-sm transition-all duration-300',
                    isDone ? 'bg-teal' : isActive ? 'bg-blue' : 'bg-blue/40')}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <span className={clsx('text-[10px] font-mono w-10 text-right',
                isDone ? 'text-teal' : isActive ? 'text-blue' : 'text-muted')}>
                {d}/{total}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/* ── MCTS 阶段涉及的步骤 ID（主图 + 子图） ─────────────────────────────────── */

const MCTS_STEP_IDS = [
  // mcts_run.json 主图步骤
  'root_node', 'tree_text', 'fanout', 'expand', 'build_children',
  'gate', 'update_state', 'apply_best',
  // mcts_single_combo.json 子图步骤
  ...MCTS_COMBO_SUB_STEPS,
]

/* ── 主组件 ───────────────────────────────────────────────────────────────────── */

export default function Phase4Mcts() {
  const mctsNodes        = useRunStore(s => s.mctsNodes)
  const mctsFrontier     = useRunStore(s => s.mctsFrontier)
  const mctsGeneration   = useRunStore(s => s.mctsGeneration)
  const metrics          = useRunStore(s => s.metrics)
  const stepOutputs      = useRunStore(s => s.stepOutputs)
  const stepLogs         = useRunStore(s => s.stepLogs)
  const mctsEvalProgress = useRunStore(s => s.mctsEvalProgress)
  const mctsLoopIter     = useRunStore(s => s.mctsLoopIter)
  const outputDetails    = useRunStore(s => s.outputDetails)
  const [showLogs, setShowLogs] = useState(false)

  const bestNode = Object.values(mctsNodes).reduce<MctsNode | null>((best, n) => {
    if (n.score < -100) return best
    return (!best || n.score > best.score) ? n : best
  }, null)

  const consecutiveBad = ((stepOutputs['update_state'] as Record<string, unknown> | undefined)?.consecutive_bad as number) ?? 0

  return (
    <div className="flex flex-col gap-0">
      <div className="grid grid-cols-1 lg:grid-cols-[35%_35%_30%] gap-4 min-h-[400px]">
        {/* Left: tree */}
        <Card>
          <h3 className="m-0 mb-3 text-sm text-primary">搜索树</h3>
          <MctsTreeViz nodes={mctsNodes} frontier={mctsFrontier} bestNode={bestNode} />
        </Card>

        {/* Center: eval */}
        <Card>
          <h3 className="m-0 mb-3 text-sm">
            第 {mctsGeneration} 代评估
            {mctsLoopIter > 0 && <span className="text-muted text-[11px] font-normal ml-2">(累计 {mctsLoopIter} 轮)</span>}
          </h3>
          <div className="mb-4">
            {mctsFrontier.map((id, i) => {
              const n = mctsNodes[id]
              return (
                <div key={id} className="flex justify-between items-center px-3 py-2 mb-1.5 bg-elevated rounded-md">
                  <span className="text-secondary text-xs">Beam {i + 1}: {id.slice(0, 6)}</span>
                  {n ? <span className="font-bold font-mono text-teal">{n.score.toFixed(3)}</span>
                    : <span className="animate-spin inline-block">{'\u2699\uFE0F'}</span>}
                </div>
              )
            })}
            {mctsFrontier.length === 0 && <div className="text-muted text-[13px]">初始化中...</div>}
          </div>
          {/* 子图透传：Combo 评估流水线进度 */}
          <EvalPipeline progress={mctsEvalProgress} />
          <ScoreHistory nodes={mctsNodes} baseline={metrics.baseline ?? 0} />
          <div className="flex gap-3 mt-3">
            <div className="flex-1 bg-elevated rounded-md px-3 py-2">
              <div className="text-[11px] text-muted">连续无改善</div>
              <div className="font-bold font-mono">{consecutiveBad} / 3</div>
            </div>
            <div className="flex-1 bg-elevated rounded-md px-3 py-2">
              <div className="text-[11px] text-muted">全局最优</div>
              <div className="font-bold font-mono text-teal">{bestNode ? bestNode.score.toFixed(4) : '\u2014'}</div>
            </div>
          </div>
        </Card>

        {/* Right: gate */}
        <GatePanel generation={mctsGeneration} nodes={mctsNodes} />
      </div>

      {/* Log toggle */}
      <div className="mt-3 border border-subtle rounded-lg overflow-hidden">
        <button onClick={() => setShowLogs(v => !v)}
          className="w-full px-4 py-2 bg-elevated border-none text-muted cursor-pointer text-left text-[13px]">
          {showLogs ? '\u25B2' : '\u25BC'} 沙盒日志
        </button>
        {showLogs && (
          <div className="p-3 bg-base max-h-[200px] overflow-y-auto font-mono text-[11px] text-secondary leading-relaxed">
            {(stepLogs['baseline'] ?? []).join('\n') || '暂无日志'}
          </div>
        )}
      </div>

      {/* LLM / 工具输出详情 */}
      <OutputDetailPanel
        details={outputDetails}
        filterStepIds={MCTS_STEP_IDS}
        title="MCTS LLM / 工具输出"
      />
    </div>
  )
}
