export type PaidWorkControlField =
  | 'emergency_stop'
  | 'max_concurrent_paid_runs'
  | 'engineering_executor_override'
  | 'qa_executor_override'

export function requiresPaidWorkControlConfirmation(field: PaidWorkControlField): boolean {
  return field !== 'max_concurrent_paid_runs'
}
