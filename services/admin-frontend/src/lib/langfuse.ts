/**
 * Build the Langfuse URL using the current hostname + direct port.
 *
 * Langfuse runs on port 3002. Proxying through nginx doesn't work well
 * because Next.js hardcodes absolute asset/API paths without base prefix.
 */
export function langfuseUrl(path = '/'): string {
  const host = window.location.hostname
  const proto = window.location.protocol
  return `${proto}//${host}:3002${path}`
}
