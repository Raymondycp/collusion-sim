import type { components } from './schema'

type S = components['schemas']

export type SimEvent = S['SimEvent']
export type SimEventType = SimEvent['type']
export type EventOf<T extends SimEventType> = Extract<SimEvent, { type: T }>
export type EpisodeHeader = S['EpisodeHeader']
export type RunStatus = S['RunStatus']
export type RunKind = S['RunKind']
export type RunSummary = S['RunSummary']
export type RunDetail = S['RunDetail']
export type Labels = S['Labels']
export type Split = S['Split']
export type Problem = S['Problem']
export type ObservationRecord = S['ObservationRecord']
export type ChatMessage = S['ChatMessage']
export type Condition = S['Condition']
export type ScenarioSummary = S['ScenarioSummary']
export type Scenario = S['Scenario']
export type Intervention = S['Intervention']
export type ChartMarkerKind = S['ChartMarker']['kind']

export type WsServerMessage = S['WsServerMessage']
export type WsClientMessage = S['WsClientMessage']
export type IndicatorUpdate = Extract<WsServerMessage, { type: 'indicator_update' }>
