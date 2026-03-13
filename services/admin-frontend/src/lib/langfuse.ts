/**
 * Build the Langfuse base URL using the current hostname.
 *
 * Langfuse is a Next.js app — proxying through `/langfuse/` breaks asset
 * loading because `NEXT_PUBLIC_BASE_PATH` is a build-time variable and the
 * prebuilt image doesn't support it. We use the direct port instead.
 */
export function langfuseUrl(path = '/'): string {
  const host = window.location.hostname
  const proto = window.location.protocol
  return `${proto}//${host}:3002${path}`
}
