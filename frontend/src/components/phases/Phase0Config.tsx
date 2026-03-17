import { useState, useEffect } from 'react'
import { useRunStore } from '@/store/runStore'
import { DEFAULT_CONFIG, type RunConfig, type CheckpointEntry } from '@/types'
import { Button, Section, Field, NumericInput, Card, Badge, inputClass } from '@/components/ui'
import { clsx } from 'clsx'

interface SavedConfigEntry {
  filename: string; run_id: string; created_at: string; requirement: string
}

interface PreflightItem {
  key: string; label: string; hint: string; required: boolean
}

const PRESETS: { label: string; desc: string; values: Partial<RunConfig> }[] = [
  { label: '快速测试', desc: 'beam=2 / 5代 / pop=4 / 500次评估',
    values: { beam_width: 2, max_generations: 5, pop_size: 4, max_fe: 500, sandbox_timeout: 60 } },
  { label: '标准运行', desc: 'beam=3 / 50代 / pop=10 / 5000次评估', values: DEFAULT_CONFIG },
  { label: '深度搜索', desc: 'beam=5 / 100代 / pop=20 / 20000次评估',
    values: { beam_width: 5, max_generations: 100, pop_size: 20, max_fe: 20000, consecutive_bad_limit: 5 } },
]

export default function Phase0Config() {
  const startRun = useRunStore(s => s.startRun)
  const [cfg, setCfg] = useState<RunConfig>({ ...DEFAULT_CONFIG })
  const [selectedPreset, setSelectedPreset] = useState(1)
  const [loading, setLoading] = useState(false)
  const [sections, setSections] = useState({ core: true, search: true, evolve: true, existing: false, paper: false, vectordb: false, model: false, resume: false })
  const [checkpoints, setCheckpoints] = useState<CheckpointEntry[]>([])
  const [loadingCkpt, setLoadingCkpt] = useState(false)
  const [selectedCkpt, setSelectedCkpt] = useState('')
  const [savedConfigs, setSavedConfigs] = useState<SavedConfigEntry[]>([])
  const [deletingFile, setDeletingFile] = useState<string | null>(null)
  const [preflightItems, setPreflightItems] = useState<PreflightItem[]>([])
  const [preflightChecked, setPreflightChecked] = useState(false)
  const [preflightLoading, setPreflightLoading] = useState(false)
  // 后端 settings 中已配置的密钥标记
  const [savedSecrets, setSavedSecrets] = useState<Record<string, boolean>>({})

  useEffect(() => {
    fetch('/api/run-configs').then(r => r.json()).then(d => setSavedConfigs(d.configs ?? [])).catch(() => {})
    // 从后端加载 settings.toml / .secrets.toml 中已配置的默认值
    fetch('/api/defaults').then(r => r.json()).then((defaults: Record<string, unknown>) => {
      // 非密钥字段合并到表单初始值（仅覆盖空值或与 DEFAULT_CONFIG 相同的值）
      const overrides: Partial<RunConfig> = {}
      for (const [k, v] of Object.entries(defaults)) {
        if (k.startsWith('has_')) continue  // 密钥标记字段跳过
        if (v && k in DEFAULT_CONFIG) {
          (overrides as Record<string, unknown>)[k] = v
        }
      }
      if (Object.keys(overrides).length > 0) {
        setCfg(c => ({ ...c, ...overrides }))
      }
      // 密钥标记：has_openai_api_key / has_pdf_md_token / has_embedding_api_key
      const secrets: Record<string, boolean> = {}
      for (const [k, v] of Object.entries(defaults)) {
        if (k.startsWith('has_') && typeof v === 'boolean') {
          secrets[k.slice(4)] = v  // 去掉 "has_" 前缀
        }
      }
      setSavedSecrets(secrets)
    }).catch(() => {})
  }, [])

  // 配置变化时清除预检结果，需要重新检查
  useEffect(() => { setPreflightChecked(false); setPreflightItems([]) }, [
    cfg.pdf_transform_mode, cfg.embedding_model_source, cfg.reranker_model_source,
    cfg.expand_references, cfg.s2_api_key, cfg.unpaywall_email,
    cfg.openai_api_key, cfg.pdf_md_token, cfg.embedding_api_key,
  ])

  const set = (key: keyof RunConfig, val: RunConfig[typeof key]) => setCfg(c => ({ ...c, [key]: val }))
  const toggle = (k: keyof typeof sections) => setSections(s => ({ ...s, [k]: !s[k] }))
  const applyPreset = (i: number) => { setSelectedPreset(i); setCfg(c => ({ ...c, ...PRESETS[i].values })) }

  const runPreflight = async (): Promise<boolean> => {
    setPreflightLoading(true)
    try {
      const res = await fetch('/api/preflight', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pdf_transform_mode: cfg.pdf_transform_mode,
          embedding_model_source: cfg.embedding_model_source,
          reranker_model_source: cfg.reranker_model_source,
          expand_references: cfg.expand_references,
          s2_api_key: cfg.s2_api_key,
          unpaywall_email: cfg.unpaywall_email,
          openai_api_key: cfg.openai_api_key,
          pdf_md_token: cfg.pdf_md_token,
          embedding_api_key: cfg.embedding_api_key,
        }),
      })
      const data = await res.json()
      setPreflightItems(data.missing ?? [])
      setPreflightChecked(true)
      return data.ok as boolean
    } catch {
      // 预检接口不可用时不阻塞启动
      setPreflightChecked(true)
      return true
    } finally {
      setPreflightLoading(false)
    }
  }

  const handleLaunch = async () => {
    // 先执行预检
    const ok = await runPreflight()
    if (!ok) return  // 有必填项缺失，不启动
    setLoading(true)
    try { await startRun(cfg) } finally { setLoading(false) }
  }

  const scanCheckpoints = async () => {
    if (!cfg.output_dir) return
    setLoadingCkpt(true)
    try {
      const res = await fetch(`/api/checkpoints?output_dir=${encodeURIComponent(cfg.output_dir)}`)
      const data = await res.json()
      setCheckpoints(data.checkpoints ?? [])
    } finally { setLoadingCkpt(false) }
  }

  const applyCheckpoint = (dir: string) => { setSelectedCkpt(dir); set('resume_checkpoint_dir', dir) }
  const clearCheckpoint = () => { setSelectedCkpt(''); set('resume_checkpoint_dir', '') }

  // 密钥字段：加载存档时不覆盖用户已填的值（密钥不保存在存档中）
  const SECRET_KEYS = ['openai_api_key', 'pdf_md_token', 'embedding_api_key'] as const

  const handleLoadConfig = async (filename: string) => {
    try {
      const res = await fetch(`/api/run-configs/${filename}`)
      const data = await res.json()
      if (data.config) {
        // 剔除密钥字段，防止存档中的空值覆盖用户已填的密钥
        const loaded = { ...data.config }
        for (const k of SECRET_KEYS) delete loaded[k]
        setCfg(c => ({ ...c, ...loaded }))
        setSelectedPreset(-1)
      }
    } catch { /* ignore */ }
  }

  const handleDeleteConfig = async (filename: string) => {
    setDeletingFile(filename)
    try {
      await fetch(`/api/run-configs/${filename}`, { method: 'DELETE' })
      setSavedConfigs(cs => cs.filter(c => c.filename !== filename))
    } finally { setDeletingFile(null) }
  }

  return (
    <div className="flex flex-col lg:flex-row gap-6 max-w-[1100px] mx-auto">
      {/* Left: form */}
      <div className="flex-1 min-w-0">
        <h2 className="text-primary mb-6 text-xl font-bold">启动配置</h2>

        <Section title="核心配置" open={sections.core} onToggle={() => toggle('core')}>
          <Field label="需求描述 *">
            <textarea value={cfg.requirement} onChange={e => set('requirement', e.target.value)}
              placeholder="描述你要优化的科学计算问题..."
              className={clsx(inputClass, 'min-h-[120px] resize-y font-sans')} />
          </Field>
          <Field label="输出目录 *">
            <input value={cfg.output_dir} onChange={e => set('output_dir', e.target.value)}
              placeholder="/path/to/output" className={inputClass} />
          </Field>
          <div className="flex flex-col sm:flex-row gap-6 mt-2">
            <Field label="自动跳过架构审核">
              <label className="flex items-center gap-2.5 cursor-pointer">
                <input type="checkbox" checked={cfg.auto_approve_arch} onChange={e => set('auto_approve_arch', e.target.checked)}
                  className="w-4.5 h-4.5 accent-teal" />
                <span className="text-secondary text-xs">跳过架构 Gate</span>
              </label>
            </Field>
            <Field label="自动跳过 MCTS 决策">
              <label className="flex items-center gap-2.5 cursor-pointer">
                <input type="checkbox" checked={cfg.auto_approve_mcts} onChange={e => set('auto_approve_mcts', e.target.checked)}
                  className="w-4.5 h-4.5 accent-teal" />
                <span className="text-secondary text-xs">每代自动 continue</span>
              </label>
            </Field>
          </div>
        </Section>

        <Section title="搜索参数" open={sections.search} onToggle={() => toggle('search')}>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Field label="Beam 宽度"><NumericInput value={cfg.beam_width} onChange={v => set('beam_width', v)} min={1} max={10} /></Field>
            <Field label="最大代数"><NumericInput value={cfg.max_generations} onChange={v => set('max_generations', v)} min={1} max={200} /></Field>
            <Field label="连续无改善回滚阈值"><NumericInput value={cfg.consecutive_bad_limit} onChange={v => set('consecutive_bad_limit', v)} min={1} max={10} /></Field>
            <Field label="沙盒超时（秒）"><NumericInput value={cfg.sandbox_timeout} onChange={v => set('sandbox_timeout', v)} min={30} max={3600} step={30} /></Field>
          </div>
        </Section>

        <Section title="进化参数" open={sections.evolve} onToggle={() => toggle('evolve')}>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Field label="种群大小"><NumericInput value={cfg.pop_size} onChange={v => set('pop_size', v)} min={2} max={50} /></Field>
            <Field label="最大函数评估次数"><NumericInput value={cfg.max_fe} onChange={v => set('max_fe', v)} min={100} max={100000} step={500} /></Field>
          </div>
          <Field label={`变异比例: ${cfg.mutation_rate.toFixed(2)}`}>
            <input type="range" min={0} max={1} step={0.05} value={cfg.mutation_rate}
              onChange={e => set('mutation_rate', Number(e.target.value))}
              className="w-full accent-blue" />
          </Field>
        </Section>

        <Section title="已有代码配置（可选）" open={sections.existing} onToggle={() => toggle('existing')}>
          <p className="text-muted text-xs mb-3">若已有现成项目代码，填写此项将跳过架构生成。</p>
          <Field label="现有项目根目录">
            <input value={cfg.existing_project_root} onChange={e => set('existing_project_root', e.target.value)}
              placeholder="/path/to/existing/project" className={inputClass} />
          </Field>
          <Field label="优化方向">
            <select value={cfg.existing_direction} onChange={e => set('existing_direction', e.target.value)}
              className={clsx(inputClass, 'cursor-pointer')}>
              <option value="maximize">maximize</option>
              <option value="minimize">minimize</option>
            </select>
          </Field>
        </Section>

        <Section title="论文写作参数" open={sections.paper} onToggle={() => toggle('paper')}>
          <Field label="LaTeX 模板路径"><input value={cfg.tex_template_path} onChange={e => set('tex_template_path', e.target.value)} className={inputClass} /></Field>
          <Field label="初始 .bib 文件路径"><input value={cfg.reference_bib_path} onChange={e => set('reference_bib_path', e.target.value)} className={inputClass} /></Field>
          <Field label="Semantic Scholar API Key">
            <input type="password" value={cfg.s2_api_key} onChange={e => set('s2_api_key', e.target.value)} className={inputClass} />
          </Field>
          <Field label="Unpaywall Email">
            <input type="email" value={cfg.unpaywall_email} onChange={e => set('unpaywall_email', e.target.value)} className={inputClass} placeholder="用于 Unpaywall API 下载论文" />
          </Field>
          <div className="flex flex-col sm:flex-row gap-4">
            <Field label="目标参考文献数"><NumericInput value={cfg.all_paper_num} onChange={v => set('all_paper_num', v)} min={10} max={200} step={10} /></Field>
            <Field label="扩充文献">
              <label className="flex items-center gap-2.5 pt-2 cursor-pointer">
                <input type="checkbox" checked={cfg.expand_references} onChange={e => set('expand_references', e.target.checked)} className="w-4.5 h-4.5 accent-teal" />
                <span className="text-secondary">调用 Semantic Scholar 扩充</span>
              </label>
            </Field>
          </div>
        </Section>

        <Section title="向量数据库参数" open={sections.vectordb} onToggle={() => toggle('vectordb')}>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Field label="嵌入模型"><input value={cfg.embedding_model} onChange={e => set('embedding_model', e.target.value)} className={inputClass} /></Field>
            <Field label="嵌入模型来源">
              <select value={cfg.embedding_model_source} onChange={e => set('embedding_model_source', e.target.value)} className={clsx(inputClass, 'cursor-pointer')}>
                <option value="api">api</option><option value="local">local</option>
              </select>
            </Field>
            <Field label="重排模型路径"><input value={cfg.reranker_path} onChange={e => set('reranker_path', e.target.value)} className={inputClass} /></Field>
            <Field label="重排模型来源">
              <select value={cfg.reranker_model_source} onChange={e => set('reranker_model_source', e.target.value)} className={clsx(inputClass, 'cursor-pointer')}>
                <option value="api">api</option><option value="local">local</option>
              </select>
            </Field>
            <Field label="数据库设备">
              <select value={cfg.database_device} onChange={e => set('database_device', e.target.value)} className={clsx(inputClass, 'cursor-pointer')}>
                <option value="cpu">cpu</option><option value="cuda:0">cuda:0</option><option value="cuda:1">cuda:1</option>
              </select>
            </Field>
            <Field label="PDF 解析模式">
              <select value={cfg.pdf_transform_mode} onChange={e => set('pdf_transform_mode', e.target.value)} className={clsx(inputClass, 'cursor-pointer')}>
                <option value="api">api</option><option value="local">local</option>
              </select>
            </Field>
          </div>
        </Section>

        <Section title="模型与密钥配置" open={sections.model} onToggle={() => toggle('model')}>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Field label="分析模型"><input value={cfg.text_model_name} onChange={e => set('text_model_name', e.target.value)} className={inputClass} /></Field>
            <Field label="写作模型"><input value={cfg.write_model_name} onChange={e => set('write_model_name', e.target.value)} className={inputClass} /></Field>
          </div>
          <Field label="OpenAI API Key *">
            <input type="password" value={cfg.openai_api_key} onChange={e => set('openai_api_key', e.target.value)}
              placeholder={savedSecrets.openai_api_key ? '已从 .secrets.toml 加载，留空即可' : 'sk-...'} className={inputClass} />
            <span className="text-muted text-[11px]">
              LLM 调用必需；留空则使用已保存的配置
              {savedSecrets.openai_api_key && <span className="text-teal ml-1.5 font-semibold">(.secrets.toml 已配置)</span>}
            </span>
          </Field>
          <Field label="PDF 解析 Token（MineRU）">
            <input type="password" value={cfg.pdf_md_token} onChange={e => set('pdf_md_token', e.target.value)}
              placeholder={savedSecrets.pdf_md_token ? '已从 .secrets.toml 加载，留空即可' : 'PDF 解析模式为 api 时必填'} className={inputClass} />
            <span className="text-muted text-[11px]">
              PDF 解析模式为 api 时必需；留空则使用已保存的配置
              {savedSecrets.pdf_md_token && <span className="text-teal ml-1.5 font-semibold">(.secrets.toml 已配置)</span>}
            </span>
          </Field>
          <Field label="嵌入/重排模型 API Key">
            <input type="password" value={cfg.embedding_api_key} onChange={e => set('embedding_api_key', e.target.value)}
              placeholder={savedSecrets.embedding_api_key ? '已从 .secrets.toml 加载，留空即可' : '嵌入或重排模型来源为 api 时必填'} className={inputClass} />
            <span className="text-muted text-[11px]">
              嵌入/重排来源为 api 时必需；留空则使用已保存的配置
              {savedSecrets.embedding_api_key && <span className="text-teal ml-1.5 font-semibold">(.secrets.toml 已配置)</span>}
            </span>
          </Field>
        </Section>

        <Section title="断点恢复（可选）" open={sections.resume} onToggle={() => toggle('resume')}>
          <p className="text-muted text-xs mb-3">若之前的运行被中断，可以从断点继续。需先填写上方"输出目录"。</p>
          <div className="flex gap-2 mb-3">
            <Button variant="ghost" size="sm" onClick={scanCheckpoints} disabled={!cfg.output_dir || loadingCkpt}>
              {loadingCkpt ? '扫描中...' : '扫描可用断点'}
            </Button>
            {selectedCkpt && (
              <Button variant="danger" size="sm" onClick={clearCheckpoint}>取消断点</Button>
            )}
          </div>

          {selectedCkpt && (
            <div className="px-3.5 py-2.5 bg-blue/10 border border-blue rounded-md mb-3 text-xs">
              <span className="text-blue font-semibold">已选择断点</span>
              <span className="text-muted ml-2 font-mono">{selectedCkpt.split('/').slice(-2).join('/')}</span>
            </div>
          )}

          {checkpoints.length > 0 && (
            <div className="flex flex-col gap-2">
              {checkpoints.map(ck => {
                const isSelected = selectedCkpt === ck.checkpoint_dir
                const dt = new Date(ck.last_updated * 1000)
                const dateStr = `${dt.getMonth()+1}/${dt.getDate()} ${dt.getHours()}:${String(dt.getMinutes()).padStart(2,'0')}`
                return (
                  <div key={ck.run_id} onClick={() => applyCheckpoint(ck.checkpoint_dir)}
                    className={clsx('p-3 rounded-lg cursor-pointer border transition-all',
                      isSelected ? 'border-blue bg-blue/10' : 'border-subtle bg-elevated hover:border-strong')}>
                    <div className="flex justify-between items-center mb-1.5">
                      <span className="font-mono text-xs text-primary font-semibold">运行 {ck.run_id}</span>
                      <span className="text-[11px] text-muted">{dateStr}</span>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {ck.completed_steps.map(s => (
                        <Badge key={s} color="teal">{s.replace(/_/g, ' ')}</Badge>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
          {checkpoints.length === 0 && !loadingCkpt && cfg.output_dir && (
            <div className="text-muted text-xs text-center py-3">点击"扫描可用断点"查找历史运行</div>
          )}
        </Section>

        {/* 预检结果 */}
        {preflightChecked && preflightItems.length > 0 && (
          <div className="mt-4 mb-2 rounded-lg border border-danger/40 bg-danger/5 overflow-hidden">
            <div className="px-4 py-2.5 bg-danger/10 border-b border-danger/20 flex items-center gap-2">
              <span className="text-danger text-sm font-semibold">
                {preflightItems.filter(i => i.required).length > 0
                  ? `发现 ${preflightItems.filter(i => i.required).length} 项必需配置缺失，请在上方表单中填写`
                  : '以下配置建议补充（非必需）'}
              </span>
            </div>
            <div className="divide-y divide-danger/10">
              {preflightItems.map(item => (
                <div key={item.key} className="px-4 py-3 flex flex-col gap-1">
                  <div className="flex items-center gap-2">
                    <span className={clsx(
                      'inline-block w-1.5 h-1.5 rounded-full shrink-0',
                      item.required ? 'bg-danger' : 'bg-yellow-500'
                    )} />
                    <span className="text-primary text-[13px] font-semibold">{item.label}</span>
                    <code className="text-[11px] text-muted font-mono bg-elevated px-1.5 py-0.5 rounded">{item.key}</code>
                    {item.required
                      ? <span className="text-[10px] text-danger font-medium">必需</span>
                      : <span className="text-[10px] text-yellow-500 font-medium">建议</span>}
                  </div>
                  <span className="text-muted text-xs ml-3.5">{item.hint}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="flex gap-2 mt-2">
          <Button variant="ghost" size="lg" onClick={runPreflight}
            disabled={preflightLoading}
            className="shrink-0">
            {preflightLoading ? '检查中...' : '检查配置'}
          </Button>
          <Button variant="gradient" size="lg" onClick={handleLaunch}
            disabled={loading || !cfg.requirement || !cfg.output_dir || preflightLoading}
            className="flex-1"
            style={{ background: loading ? 'var(--color-elevated)' : 'linear-gradient(90deg, var(--color-blue), var(--color-teal))' }}>
            {loading ? '启动中...' : selectedCkpt ? '从断点恢复运行' : '启动全流程'}
          </Button>
        </div>
      </div>

      {/* Right sidebar: presets + history */}
      <div className="w-full lg:w-[260px] shrink-0">
        <h3 className="text-secondary mb-4 text-sm font-semibold uppercase tracking-wide">参数预设</h3>
        <div className="flex flex-col gap-3">
          {PRESETS.map((p, i) => (
            <div key={i} onClick={() => applyPreset(i)}
              className={clsx('p-3.5 rounded-lg cursor-pointer border transition-all',
                selectedPreset === i ? 'border-blue bg-blue/10' : 'border-subtle bg-surface hover:border-strong')}>
              <div className="font-semibold mb-1">{p.label}</div>
              <div className="text-muted text-xs">{p.desc}</div>
            </div>
          ))}
        </div>

        {/* History */}
        <div className="mt-6">
          <h3 className="text-secondary mb-3 text-sm font-semibold uppercase tracking-wide">历史配置</h3>
          {savedConfigs.length === 0 ? (
            <div className="text-muted text-xs text-center py-3">首次启动后自动保存</div>
          ) : (
            <div className="flex flex-col gap-2 max-h-80 overflow-y-auto">
              {savedConfigs.map(sc => {
                const dt = sc.created_at ? new Date(sc.created_at) : null
                const dateStr = dt ? `${dt.getMonth()+1}/${dt.getDate()} ${dt.getHours()}:${String(dt.getMinutes()).padStart(2,'0')}` : sc.run_id
                const reqPreview = sc.requirement ? sc.requirement.slice(0, 40) + (sc.requirement.length > 40 ? '...' : '') : '（无描述）'
                return (
                  <Card key={sc.filename} className="!p-2.5">
                    <div className="flex justify-between items-center mb-1">
                      <span className="font-mono text-[11px] text-muted">{dateStr}</span>
                      <button onClick={() => handleDeleteConfig(sc.filename)} disabled={deletingFile === sc.filename}
                        className="bg-transparent border-none text-muted cursor-pointer px-0.5 text-[13px] leading-none hover:text-danger" aria-label="Delete">
                        &#10005;
                      </button>
                    </div>
                    <div className="text-xs text-secondary mb-2 leading-relaxed">{reqPreview}</div>
                    <Button variant="ghost" size="sm" className="w-full" onClick={() => handleLoadConfig(sc.filename)}>
                      载入此配置
                    </Button>
                  </Card>
                )
              })}
            </div>
          )}
        </div>

        {/* Quick params view */}
        <Card className="mt-6">
          <div className="text-xs text-muted mb-3 font-semibold uppercase">当前参数快览</div>
          {([['Beam 宽度', cfg.beam_width], ['最大代数', cfg.max_generations],
            ['种群大小', cfg.pop_size], ['最大评估', cfg.max_fe]] as [string, number][]).map(([label, val]) => (
            <div key={label} className="flex justify-between mb-1.5">
              <span className="text-secondary text-[13px]">{label}</span>
              <span className="text-blue font-mono text-[13px]">{val}</span>
            </div>
          ))}
        </Card>
      </div>
    </div>
  )
}
