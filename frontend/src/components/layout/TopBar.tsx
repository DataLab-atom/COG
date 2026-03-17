import { useState } from 'react'
import { Hexagon, Square, Menu, WifiOff } from 'lucide-react'
import { useRunStore } from '@/store/runStore'
import { Button, Modal } from '@/components/ui'
import { clsx } from 'clsx'

function formatElapsed(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  const parts: string[] = []
  if (h > 0) parts.push(`${h}h`)
  if (m > 0 || h > 0) parts.push(`${m}m`)
  parts.push(`${s}s`)
  return parts.join(' ')
}

export default function TopBar({ onMenuToggle }: { onMenuToggle: () => void }) {
  const runId = useRunStore(s => s.runId)
  const elapsed = useRunStore(s => s.elapsed)
  const progress = useRunStore(s => s.progress)
  const gateState = useRunStore(s => s.gateState)
  const wsConnected = useRunStore(s => s.wsConnected)
  const abortRun = useRunStore(s => s.abortRun)
  const [showConfirm, setShowConfirm] = useState(false)

  const isGateActive = gateState !== null

  return (
    <>
      <header className="fixed top-0 left-0 right-0 h-12 bg-surface border-b border-subtle z-50 flex items-center px-4 gap-3">
        {/* Mobile menu button */}
        <button
          onClick={onMenuToggle}
          className="md:hidden p-1 text-secondary cursor-pointer bg-transparent border-none"
          aria-label="Toggle sidebar"
        >
          <Menu size={20} />
        </button>

        {/* Logo */}
        <div className="flex items-center gap-2 min-w-[140px]">
          <Hexagon size={22} className="text-blue shrink-0" strokeWidth={2} />
          <span className="text-primary font-bold text-[15px] tracking-tight whitespace-nowrap">
            Science-OS
          </span>
        </div>

        {/* Center: progress */}
        <div className="flex-1 flex flex-col items-center justify-center gap-1 min-w-0">
          {runId && (
            <div className="flex items-center gap-3 w-full max-w-[600px]">
              <span className="text-muted text-[11px] whitespace-nowrap font-mono hidden sm:inline">
                {runId.slice(0, 8)}
              </span>
              {!wsConnected && (
                <span className="flex items-center gap-1 text-amber text-[11px] whitespace-nowrap animate-pulse">
                  <WifiOff size={12} /> 重连中
                </span>
              )}
              <span className="text-secondary text-xs whitespace-nowrap">
                {formatElapsed(elapsed)}
              </span>
              <div className="flex-1 h-1 bg-elevated rounded-sm overflow-hidden">
                <div
                  className={clsx('h-full rounded-sm transition-all duration-600', isGateActive && 'animate-pulse-ring')}
                  style={{
                    width: `${progress}%`,
                    background: isGateActive
                      ? 'var(--color-amber)'
                      : 'linear-gradient(90deg, var(--color-blue), var(--color-teal))',
                  }}
                />
              </div>
              <span className="text-muted text-[11px] whitespace-nowrap">{progress}%</span>
            </div>
          )}
        </div>

        {/* Right: abort */}
        <div className="min-w-[100px] md:min-w-[140px] flex justify-end">
          {runId && (
            <Button variant="danger" size="sm" onClick={() => setShowConfirm(true)}>
              <Square size={12} /> <span className="hidden sm:inline">中止运行</span>
            </Button>
          )}
        </div>
      </header>

      {/* Confirm dialog */}
      <Modal open={showConfirm} onClose={() => setShowConfirm(false)} maxWidth="400px">
        <p className="text-primary text-[15px] font-semibold mb-2">确认中止运行？</p>
        <p className="text-secondary text-[13px] mb-6">
          中止后当前运行将停止。已完成的步骤已自动保存为断点，可在配置页扫描并从断点恢复。
        </p>
        <div className="flex gap-2.5 justify-end">
          <Button variant="ghost" onClick={() => setShowConfirm(false)}>取消</Button>
          <Button variant="danger" onClick={async () => { setShowConfirm(false); await abortRun() }}
            style={{ background: 'var(--color-danger)', color: '#fff', borderColor: 'transparent' }}>
            确认中止
          </Button>
        </div>
      </Modal>
    </>
  )
}
