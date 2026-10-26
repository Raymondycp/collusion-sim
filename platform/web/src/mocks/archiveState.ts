/** Which records the mock server has archived (DECISIONS-29, contract 0.39.0): id to the time of the archive. */
export const archivedRuns = new Map<string, string>()
export const archivedExperiments = new Map<string, string>()

export function resetMockArchive() {
  archivedRuns.clear()
  archivedExperiments.clear()
}

export const ARCHIVED_AT = '2026-10-23T15:00:00Z'

export type ArchiveBody = { ids?: string[]; all?: true }
