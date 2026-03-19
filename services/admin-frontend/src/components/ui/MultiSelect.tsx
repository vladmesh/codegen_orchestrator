import { useState, useRef, useEffect } from 'react'

interface MultiSelectProps {
  options: string[]
  selected: string[]
  onChange: (selected: string[]) => void
  placeholder: string
  formatLabel?: (value: string) => string
}

export function MultiSelect({
  options,
  selected,
  onChange,
  placeholder,
  formatLabel = (v) => v.replace(/_/g, ' '),
}: MultiSelectProps) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  const toggle = (value: string) => {
    if (selected.includes(value)) {
      onChange(selected.filter((s) => s !== value))
    } else {
      onChange([...selected, value])
    }
  }

  const label =
    selected.length === 0
      ? placeholder
      : selected.length === 1
        ? formatLabel(selected[0])
        : `${selected.length} selected`

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground hover:bg-muted/50"
      >
        <span>{label}</span>
        <svg
          className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-180' : ''}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="absolute z-10 mt-1 min-w-[180px] rounded-md border border-border bg-background shadow-lg">
          <div
            className="cursor-pointer px-3 py-1.5 text-xs text-muted-foreground hover:bg-muted/50"
            onClick={() => onChange([])}
          >
            Clear all
          </div>
          <div className="border-t border-border" />
          {options.map((opt) => (
            <label
              key={opt}
              className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm hover:bg-muted/50"
            >
              <input
                type="checkbox"
                checked={selected.includes(opt)}
                onChange={() => toggle(opt)}
                className="rounded border-border"
              />
              <span>{formatLabel(opt)}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
