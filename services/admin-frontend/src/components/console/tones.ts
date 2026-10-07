import type { DotTone } from './Chips'

export function containerTone(status: string): DotTone {
  if (status === 'running') return 'ok'
  if (status === 'down') return 'down'
  if (status === 'deploying') return 'active'
  if (status === 'not_deployed' || status === 'stopped') return 'idle'
  return 'warn'
}
