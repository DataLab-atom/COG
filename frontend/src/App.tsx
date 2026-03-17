import { useState } from 'react'
import { RotateCcw } from 'lucide-react'
import { useWebSocket } from '@/hooks/useWebSocket'
import { useRunStore } from '@/store/runStore'
import { Button } from '@/components/ui'
import TopBar from '@/components/layout/TopBar'
import SideNav from '@/components/layout/SideNav'
import Phase0Config from '@/components/phases/Phase0Config'
import Phase1ArchBuild from '@/components/phases/Phase1ArchBuild'
import Phase3CodeGen from '@/components/phases/Phase3CodeGen'
import Phase4Mcts from '@/components/phases/Phase4Mcts'
import Phase5U2e from '@/components/phases/Phase5U2e'
import Phase6Paper from '@/components/phases/Phase6Paper'
import Phase7Done from '@/components/phases/Phase7Done'
import { ArchGateModal } from '@/components/phases/Phase1ArchBuild'

function PhaseContent({ phase }: { phase: number }) {
  switch (phase) {
    case 0:  return <Phase0Config />
    case 1:
    case 2:  return <Phase1ArchBuild />
    case 3:  return <Phase3CodeGen />
    case 4:  return <Phase4Mcts />
    case 5:  return <Phase5U2e />
    case 6:  return <Phase6Paper />
    case 7:  return <Phase7Done />
    default: return null
  }
}

export default function App() {
  useWebSocket()
  const phase = useRunStore(s => s.phase)
  const runError = useRunStore(s => s.runError)
  const gateState = useRunStore(s => s.gateState)
  const reset = useRunStore(s => s.reset)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  return (
    <>
      <TopBar onMenuToggle={() => setSidebarOpen(v => !v)} />
      <SideNav open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      {/* 架构 Gate 弹窗提升到顶层，确保任何 Phase 都能弹出 */}
      {gateState?.gate_type === 'arch' && <ArchGateModal />}

      <main className="mt-12 md:ml-[220px] p-4 md:p-6 min-h-[calc(100vh-48px)] bg-base overflow-y-auto">
        {runError && (
          <div className="w-full mb-5 px-4 py-3 bg-danger/10 border border-danger rounded-lg text-danger text-[13px] font-medium flex items-center justify-between gap-3" role="alert">
            <span>{runError}</span>
            <Button variant="ghost" size="sm" onClick={reset} className="shrink-0 text-danger">
              <RotateCcw size={13} /> 重新开始
            </Button>
          </div>
        )}
        <PhaseContent phase={phase} />
      </main>
    </>
  )
}
