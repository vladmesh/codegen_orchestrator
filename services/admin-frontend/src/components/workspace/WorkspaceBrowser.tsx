import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { FileTree } from './FileTree'
import { FileViewer } from './FileViewer'
import type { FileTreeEntry, FileContentResponse, WorkspaceFileContentResponse } from '@/types/api'

interface WorkspaceBrowserProps {
  treeApiUrl: string
  fileApiUrlPrefix: string
  /** Query key prefix for cache isolation */
  queryKeyPrefix: string
}

export function WorkspaceBrowser({ treeApiUrl, fileApiUrlPrefix, queryKeyPrefix }: WorkspaceBrowserProps) {
  const [selectedFile, setSelectedFile] = useState<string | null>(null)

  const { data: treeData, isLoading: treeLoading, error: treeError } = useQuery({
    queryKey: [queryKeyPrefix, 'tree'],
    queryFn: () => api.raw<FileTreeEntry[]>(treeApiUrl),
    refetchInterval: 15_000,
  })

  const { data: fileContent, isLoading: fileLoading } = useQuery({
    queryKey: [queryKeyPrefix, 'file', selectedFile],
    queryFn: () =>
      api.raw<FileContentResponse | WorkspaceFileContentResponse>(
        `${fileApiUrlPrefix}${selectedFile}`
      ),
    enabled: !!selectedFile,
  })

  if (treeLoading) return <p className="text-muted-foreground">Loading file tree...</p>

  if (treeError) {
    const status = (treeError as { status?: number })?.status
    if (status === 404) {
      return <p className="text-muted-foreground">No workspace found on disk</p>
    }
    return <p className="text-muted-foreground">Failed to load workspace</p>
  }

  return (
    <div className="flex gap-4" style={{ minHeight: 400 }}>
      {/* Tree panel */}
      <div className="w-72 shrink-0 overflow-auto rounded-lg border border-border p-2">
        <FileTree
          entries={treeData ?? []}
          selectedFile={selectedFile}
          onSelect={setSelectedFile}
        />
      </div>

      {/* File content panel */}
      <div className="flex-1 overflow-auto rounded-lg border border-border">
        <FileViewer
          path={selectedFile}
          content={fileContent?.content ?? null}
          size={fileContent?.size ?? null}
          isLoading={fileLoading}
        />
      </div>
    </div>
  )
}
