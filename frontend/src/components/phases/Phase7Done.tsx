import { useRunStore } from '@/store/runStore'
import { Card, Button } from '@/components/ui'

export default function Phase7Done() {
  const metrics     = useRunStore(s => s.metrics)
  const stepOutputs = useRunStore(s => s.stepOutputs)
  const elapsed     = useRunStore(s => s.elapsed)
  const reset       = useRunStore(s => s.reset)

  const paperTexPath = (stepOutputs['merge_writing'] as Record<string, unknown> | undefined)?.paper_tex_path as string | undefined

  const baseline = metrics.baseline ?? 0
  const mctsBest = metrics.mcts_best ?? 0
  const u2eBest  = metrics.u2e_best ?? mctsBest
  const totalImp = baseline > 0 ? ((u2eBest - baseline) / Math.abs(baseline) * 100) : 0
  const h = Math.floor(elapsed / 3600), m = Math.floor((elapsed % 3600) / 60), s = elapsed % 60
  const elapsedStr = `${h}h ${m}m ${s}s`

  return (
    <div className="max-w-[700px] mx-auto text-center">
      <div className="text-5xl mb-4" aria-hidden="true">{'\uD83C\uDF89'}</div>
      <h2 className="text-[28px] text-primary mb-2">全流程完成！</h2>
      <p className="text-secondary mb-8">所有阶段已成功执行，最优代码和论文已生成。</p>

      <Card className="mb-6 text-left !border-strong">
        <h3 className="m-0 mb-4 text-[15px]">优化结果摘要</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {([
            ['基线分数', baseline > 0 ? baseline.toFixed(4) : '\u2014', 'secondary'],
            ['MCTS 最优', mctsBest > 0 ? `${mctsBest.toFixed(4)}  (+${((mctsBest-baseline)/Math.abs(baseline||1)*100).toFixed(1)}%)` : '\u2014', 'blue'],
            ['U2E 最终最优', u2eBest > 0 ? `${u2eBest.toFixed(4)}  (+${totalImp.toFixed(1)}%) \u2605` : '\u2014', 'teal'],
            ['总运行时间', elapsedStr, 'secondary'],
            ['MCTS 搜索代数', metrics.mcts_generations ? `${metrics.mcts_generations} 代` : '\u2014', 'secondary'],
            ['U2E 函数评估', metrics.function_evals ? `${metrics.function_evals.toLocaleString()} 次` : '\u2014', 'secondary'],
          ] as [string, string, string][]).map(([label, value, color]) => (
            <div key={label} className="px-3.5 py-2.5 bg-elevated rounded-md">
              <div className="text-xs text-muted mb-1">{label}</div>
              <div className={`font-bold font-mono text-${color}`}>{value}</div>
            </div>
          ))}
        </div>
      </Card>

      <Card className="mb-6 text-left !border-strong">
        <h3 className="m-0 mb-4 text-[15px]">输出文件</h3>
        <div className="flex flex-col gap-2.5">
          {paperTexPath && (
            <div className="flex justify-between items-center px-3.5 py-2.5 bg-elevated rounded-md">
              <span className="text-secondary">{'\uD83D\uDCC4'} paper.tex</span>
              <a href={`/api/files?path=${encodeURIComponent(paperTexPath)}`} download
                className="inline-flex px-3.5 py-1 border-none rounded-md bg-teal text-base font-semibold cursor-pointer no-underline text-[13px]">
                {'\u2B07'} 下载
              </a>
            </div>
          )}
          <div className="px-3.5 py-2.5 bg-elevated rounded-md text-muted text-[13px]">
            {'\uD83D\uDCC1'} 完整输出目录 {'\u2014'} 查看配置中指定的 output_dir
          </div>
        </div>
      </Card>

      <Button variant="ghost" size="md" onClick={reset}>
        使用相同配置重新运行
      </Button>
    </div>
  )
}
