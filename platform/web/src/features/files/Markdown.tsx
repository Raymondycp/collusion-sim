import Markdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { fileUrl } from '@/api/files'
import { cn } from '@/lib/utils'

/** A relative image path in a repo file, resolved against the file's folder, served by the file route. */
function repoImage(dir: string, src: string): string {
  const parts = dir ? dir.split('/') : []
  for (const seg of src.split('/')) {
    if (seg === '..') parts.pop()
    else if (seg !== '.' && seg !== '') parts.push(seg)
  }
  return fileUrl(parts.join('/'))
}

/** Rendered markdown. Raw HTML in the source is not rendered (react-markdown default). `dir` is the repo folder of the file, for relative images. */
export function MarkdownView({ text, className, dir }: { text: string; className?: string; dir?: string }) {
  const urlTransform = (url: string, key: string) =>
    dir !== undefined && key === 'src' && !/^([a-z][a-z0-9+.-]*:|\/|#)/i.test(url) ? repoImage(dir, url) : defaultUrlTransform(url)
  return (
    <div
      className={cn(
        'text-sm leading-relaxed break-words',
        '[&_h1]:mt-4 [&_h1]:mb-2 [&_h1]:text-2xl [&_h1]:font-semibold [&_h2]:mt-4 [&_h2]:mb-2 [&_h2]:text-xl [&_h2]:font-semibold [&_h3]:mt-3 [&_h3]:mb-1 [&_h3]:text-base [&_h3]:font-semibold',
        '[&_p]:my-2 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-6 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-6 [&_li]:my-0.5',
        '[&_a]:text-primary [&_a]:underline [&_blockquote]:border-l-2 [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground',
        '[&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:text-xs [&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-muted [&_pre]:p-2 [&_pre_code]:bg-transparent [&_pre_code]:p-0',
        '[&_table]:my-2 [&_table]:border-collapse [&_td]:border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:bg-muted [&_th]:px-2 [&_th]:py-1 [&_img]:max-w-full [&_hr]:my-4',
        className,
      )}
    >
      <Markdown remarkPlugins={[remarkGfm]} urlTransform={urlTransform}>{text}</Markdown>
    </div>
  )
}
