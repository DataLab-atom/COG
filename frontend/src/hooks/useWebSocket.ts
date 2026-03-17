import { useEffect, useRef } from 'react'
import { useRunStore } from '@/store/runStore'
import type { WsEvent } from '@/types'

const MAX_RECONNECT_DELAY = 30_000
const BASE_DELAY = 1_000

export function useWebSocket() {
  const runId          = useRunStore(s => s.runId)
  const handleEvent    = useRunStore(s => s.handleEvent)
  const tickElapsed    = useRunStore(s => s.tickElapsed)
  const setWsConnected = useRunStore(s => s.setWsConnected)
  const wsRef          = useRef<WebSocket | null>(null)
  const timerRef       = useRef<ReturnType<typeof setInterval> | null>(null)
  const retryRef       = useRef(0)

  useEffect(() => {
    if (!runId) return

    timerRef.current = setInterval(tickElapsed, 1000)
    document.title = '\u2699\uFE0F \u8FD0\u884C\u4E2D \u2014 Science-OS'

    const connect = () => {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const ws = new WebSocket(`${proto}//${window.location.host}/ws/runs/${runId}/events`)
      wsRef.current = ws

      ws.onopen = () => {
        retryRef.current = 0
        setWsConnected(true)
      }

      ws.onmessage = (e) => {
        try {
          const raw = JSON.parse(e.data) as { type: string }
          if (raw.type === 'ping') return
          const event = raw as WsEvent
          handleEvent(event)

          if (event.type === 'gate_waiting') {
            document.title = '\uD83D\uDD14 \u9700\u8981\u4F60\u7684\u51B3\u7B56 \u2014 Science-OS'
          } else if (event.type === 'run_done') {
            document.title = '\u2705 \u5B8C\u6210 \u2014 Science-OS'
            if (timerRef.current) clearInterval(timerRef.current)
          } else if (event.type === 'run_error') {
            document.title = '\u274C \u9519\u8BEF \u2014 Science-OS'
          }
        } catch { /* ignore non-JSON */ }
      }

      ws.onclose = () => {
        if (wsRef.current !== ws) return
        setWsConnected(false)
        // 指数退避重连
        const delay = Math.min(BASE_DELAY * Math.pow(2, retryRef.current), MAX_RECONNECT_DELAY)
        retryRef.current += 1
        setTimeout(connect, delay)
      }

      ws.onerror = () => { ws.close() }
    }

    connect()

    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
      if (wsRef.current) {
        const ws = wsRef.current
        wsRef.current = null
        ws.close()
      }
      setWsConnected(false)
      document.title = 'Science-OS'
      retryRef.current = 0
    }
  }, [runId, handleEvent, tickElapsed, setWsConnected])
}
