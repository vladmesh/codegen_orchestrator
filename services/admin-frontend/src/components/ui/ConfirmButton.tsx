import { useState } from 'react'

const VARIANTS = {
  blue: {
    idle: 'rounded-md border border-blue-800 px-3 py-1.5 text-sm text-blue-400 hover:bg-blue-900/30',
    confirm:
      'rounded-md bg-blue-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-600 disabled:opacity-50',
  },
  red: {
    idle: 'rounded-md border border-red-800 px-3 py-1.5 text-sm text-red-400 hover:bg-red-900/30',
    confirm:
      'rounded-md bg-red-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-600 disabled:opacity-50',
  },
  green: {
    idle: 'rounded-md border border-green-800 px-3 py-1.5 text-sm text-green-400 hover:bg-green-900/30',
    confirm:
      'rounded-md bg-green-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-green-600 disabled:opacity-50',
  },
} as const

interface ConfirmButtonProps {
  label: string
  confirmText: string
  pendingLabel: string
  onConfirm: () => void
  isPending: boolean
  variant?: keyof typeof VARIANTS
  disabled?: boolean
}

export function ConfirmButton({
  label,
  confirmText,
  pendingLabel,
  onConfirm,
  isPending,
  variant = 'blue',
  disabled = false,
}: ConfirmButtonProps) {
  const [confirming, setConfirming] = useState(false)
  const styles = VARIANTS[variant]

  if (confirming) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">{confirmText}</span>
        <button
          onClick={onConfirm}
          disabled={isPending}
          className={styles.confirm}
        >
          {isPending ? pendingLabel : 'Confirm'}
        </button>
        <button
          onClick={() => setConfirming(false)}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          Cancel
        </button>
      </div>
    )
  }

  return (
    <button
      onClick={() => setConfirming(true)}
      disabled={disabled}
      className={`${styles.idle} ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      {label}
    </button>
  )
}
