export function FileViewer({
  path,
  content,
  size,
  isLoading,
}: {
  path: string | null
  content: string | null
  size: number | null
  isLoading: boolean
}) {
  if (!path) {
    return <p className="p-4 text-muted-foreground">Select a file to view its contents</p>
  }

  if (isLoading) {
    return <p className="p-4 text-muted-foreground">Loading file...</p>
  }

  if (content === null) {
    return <p className="p-4 text-muted-foreground">Could not load file</p>
  }

  return (
    <div>
      <div className="border-b border-border px-4 py-2">
        <span className="font-mono text-xs text-muted-foreground">{path}</span>
        {size !== null && (
          <span className="ml-2 text-xs text-muted-foreground">({formatSize(size)})</span>
        )}
      </div>
      <pre className="overflow-auto p-4 font-mono text-xs leading-relaxed text-foreground">
        {content}
      </pre>
    </div>
  )
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
