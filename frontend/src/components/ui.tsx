import { clsx } from 'clsx'
import React, { useEffect, useRef, type ReactNode, type ButtonHTMLAttributes } from 'react'
import type { StepStatus } from '@/types'

/* ── Button ─────────────────────────────────────────────────────────────────── */

const btnBase = 'inline-flex items-center justify-center gap-1.5 rounded-md font-medium cursor-pointer transition-all font-sans'
const btnVariants = {
  primary:   'bg-blue text-base border-none font-bold btn-hover',
  secondary: 'bg-transparent border border-strong text-secondary btn-ghost-blue',
  danger:    'bg-transparent border border-danger text-danger btn-ghost-danger',
  ghost:     'bg-transparent border border-subtle text-secondary hover:bg-elevated',
  teal:      'bg-teal text-base border-none font-bold btn-hover',
  gradient:  'border-none text-primary font-bold btn-lift',
} as const

const btnSizes = {
  sm: 'px-3 py-1 text-xs',
  md: 'px-4 py-1.5 text-[13px]',
  lg: 'px-6 py-2.5 text-base',
} as const

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: keyof typeof btnVariants
  size?: keyof typeof btnSizes
}

export function Button({ variant = 'primary', size = 'md', className, disabled, style, ...props }: ButtonProps) {
  return (
    <button
      className={clsx(btnBase, btnVariants[variant], btnSizes[size], disabled && 'opacity-50 cursor-not-allowed', className)}
      disabled={disabled}
      style={style}
      {...props}
    />
  )
}

/* ── Card ───────────────────────────────────────────────────────────────────── */

const highlightBorders: Record<string, string> = {
  blue: 'border-blue', teal: 'border-teal', amber: 'border-amber', danger: 'border-danger',
}

interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  highlight?: 'blue' | 'teal' | 'amber' | 'danger' | null
}

export function Card({ highlight, className, children, ...props }: CardProps) {
  return (
    <div
      className={clsx('bg-surface rounded-lg p-4 border', highlight ? highlightBorders[highlight] : 'border-subtle', className)}
      {...props}
    >
      {children}
    </div>
  )
}

/* ── Modal ──────────────────────────────────────────────────────────────────── */

interface ModalProps {
  open: boolean
  onClose: () => void
  children: ReactNode
  maxWidth?: string
}

export function Modal({ open, onClose, children, maxWidth = '900px' }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handler)
    dialogRef.current?.focus()
    return () => document.removeEventListener('keydown', handler)
  }, [open, onClose])

  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-[100] bg-black/60 flex items-center justify-center"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="animate-slide-down bg-surface rounded-xl p-8 overflow-auto shadow-2xl outline-none"
        style={{ width: '90%', maxWidth, maxHeight: '90vh' }}
        onClick={e => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  )
}

/* ── Badge ──────────────────────────────────────────────────────────────────── */

const badgeColors: Record<string, string> = {
  blue:   'bg-blue/15 text-blue border-blue',
  teal:   'bg-teal/15 text-teal border-teal',
  amber:  'bg-amber/15 text-amber border-amber',
  danger: 'bg-danger/15 text-danger border-danger',
  muted:  'bg-elevated text-muted border-subtle',
  purple: 'bg-purple/15 text-purple border-purple',
}

interface BadgeProps {
  color?: keyof typeof badgeColors
  children: ReactNode
  className?: string
}

export function Badge({ color = 'muted', children, className }: BadgeProps) {
  return (
    <span className={clsx('inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs border', badgeColors[color], className)}>
      {children}
    </span>
  )
}

/* ── StepIcon ───────────────────────────────────────────────────────────────── */

export function StepIcon({ status, size = 16 }: { status: StepStatus; size?: number }) {
  if (status === 'running') return <span className="animate-spin inline-block" style={{ fontSize: size }}>&#9881;</span>
  if (status === 'done')    return <span className="text-teal font-bold" style={{ fontSize: size }}>&#10003;</span>
  if (status === 'error')   return <span className="text-danger font-bold" style={{ fontSize: size }}>&#10007;</span>
  return <span className="text-strong" style={{ fontSize: size }}>&#9675;</span>
}

/* ── StatCard ───────────────────────────────────────────────────────────────── */

interface StatCardProps {
  label: string
  value: string
  sub?: string
  className?: string
}

export function StatCard({ label, value, sub, className }: StatCardProps) {
  return (
    <Card className={clsx('flex-1', className)}>
      <div className="text-xs text-muted mb-1.5">{label}</div>
      <div className="text-xl font-bold font-mono text-primary">{value}</div>
      {sub && <div className="text-xs text-teal mt-1">{sub}</div>}
    </Card>
  )
}

/* ── ProgressRing (SVG) ─────────────────────────────────────────────────────── */

export function ProgressRing({ pct, size = 60, color }: { pct: number; size?: number; color?: string }) {
  const r = (size - 6) / 2
  const circ = 2 * Math.PI * r
  const offset = circ * (1 - pct / 100)
  const c = color ?? (pct === 100 ? 'var(--color-teal)' : 'var(--color-blue)')
  return (
    <svg width={size} height={size} aria-label={`${Math.round(pct)}%`}>
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="var(--color-elevated)" strokeWidth={5} />
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke={c} strokeWidth={5}
        strokeDasharray={circ} strokeDashoffset={offset} strokeLinecap="round"
        style={{ transform: 'rotate(-90deg)', transformOrigin: `${size/2}px ${size/2}px`, transition: 'stroke-dashoffset 0.5s' }} />
      <text x={size/2} y={size/2 + 4} textAnchor="middle" fontSize={11} fill={pct === 100 ? c : 'var(--color-secondary)'}>{Math.round(pct)}%</text>
    </svg>
  )
}

/* ── Section (collapsible) ──────────────────────────────────────────────────── */

interface SectionProps {
  title: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}

export function Section({ title, open, onToggle, children }: SectionProps) {
  return (
    <div className="mb-4 border border-subtle rounded-lg overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full text-left px-4 py-2.5 bg-elevated border-none text-primary font-semibold cursor-pointer flex justify-between items-center"
        aria-expanded={open}
      >
        {title}
        <span className="text-muted">{open ? '\u25B2' : '\u25BC'}</span>
      </button>
      {open && <div className="p-4 bg-surface">{children}</div>}
    </div>
  )
}

/* ── Field (form) ───────────────────────────────────────────────────────────── */

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mb-3.5">
      <label className="block text-secondary mb-1.5 text-[13px]">{label}</label>
      {children}
    </div>
  )
}

/* ── TextInput / Textarea shared class ──────────────────────────────────────── */

export const inputClass = 'w-full bg-elevated border border-subtle rounded-md px-3 py-2 text-primary text-sm outline-none focus:border-blue transition-colors'

/* ── NumericInput ───────────────────────────────────────────────────────────── */

interface NumericInputProps {
  value: number
  onChange: (v: number) => void
  min?: number; max?: number; step?: number
}

export function NumericInput({ value, onChange, min = 1, max = 9999, step = 1 }: NumericInputProps) {
  return (
    <div className="flex gap-1">
      <button
        onClick={() => onChange(Math.max(min, value - step))}
        className="w-8 bg-overlay border border-subtle rounded-md text-primary cursor-pointer"
        aria-label="decrease"
      >-</button>
      <input
        type="number" value={value} onChange={e => onChange(Number(e.target.value))}
        min={min} max={max} step={step}
        className={clsx(inputClass, 'text-center w-[100px]')}
      />
      <button
        onClick={() => onChange(Math.min(max, value + step))}
        className="w-8 bg-overlay border border-subtle rounded-md text-primary cursor-pointer"
        aria-label="increase"
      >+</button>
    </div>
  )
}

/* ── StepRow ────────────────────────────────────────────────────────────────── */

const stepBorderColors: Record<StepStatus, string> = {
  running: 'border-l-blue', done: 'border-l-teal', error: 'border-l-danger', pending: 'border-l-subtle',
}

interface StepRowProps {
  status: StepStatus
  label: string
  extra?: ReactNode
}

export function StepRow({ status, label, extra }: StepRowProps) {
  return (
    <div className={clsx('flex items-center gap-3 px-3.5 py-2.5 bg-surface rounded-md border-l-[3px]', stepBorderColors[status])}>
      <StepIcon status={status} />
      <span className={clsx('flex-1', status === 'pending' ? 'text-muted' : 'text-primary')}>{label}</span>
      {extra}
    </div>
  )
}
