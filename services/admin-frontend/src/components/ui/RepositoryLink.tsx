import { cn, repositoryWebUrl } from '@/lib/utils'

interface RepositoryLinkProps {
  gitUrl: string | null | undefined
  className?: string
  children: React.ReactNode
}

export function RepositoryLink({ gitUrl, className, children }: RepositoryLinkProps) {
  const href = repositoryWebUrl(gitUrl)
  if (!href) {
    return <span className={cn('text-sm text-muted-foreground', className)}>Repository not created yet</span>
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={cn('text-primary hover:underline', className)}
    >
      {children}
    </a>
  )
}
