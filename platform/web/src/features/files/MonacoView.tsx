import Editor from '@monaco-editor/react'
import './monacoSetup'

/** Read-only Monaco. `path` gives each tab its own model, so a tab keeps its scroll position and folds. */
export default function MonacoView({ path, value, language, dark }: { path: string; value: string; language: string; dark: boolean }) {
  return (
    <Editor
      height="100%"
      path={path}
      language={language}
      value={value}
      theme={dark ? 'vs-dark' : 'vs'}
      loading={<p className="p-3 text-sm text-muted-foreground">Loading editor...</p>}
      options={{
        readOnly: true,
        domReadOnly: true,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        automaticLayout: true,
        wordWrap: language === 'markdown' || language === 'plaintext' ? 'on' : 'off',
        renderWhitespace: 'none',
        fontSize: 13,
      }}
    />
  )
}
