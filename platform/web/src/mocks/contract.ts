import type { paths } from '@/api/schema'

type Method = 'get' | 'post' | 'put' | 'patch' | 'delete'

/** The JSON body a path answers with, from the contract. Mock bodies use `satisfies Res<…>`, so contract drift fails tsc. */
export type Res<P extends keyof paths, M extends Method & keyof paths[P], St extends number = 200> = paths[P][M] extends { responses: infer R }
  ? St extends keyof R
    ? R[St] extends { content: { 'application/json': infer B } }
      ? B
      : never
    : never
  : never

/** The JSON body a path accepts, from the contract. */
export type Req<P extends keyof paths, M extends Method & keyof paths[P]> = paths[P][M] extends { requestBody?: { content: { 'application/json': infer B } } } ? B : never
