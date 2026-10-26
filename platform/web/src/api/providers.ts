import { useQuery } from '@tanstack/react-query'
import { api, ApiError, unwrap } from './client'
import type { components } from './schema'

type S = components['schemas']
export type ProviderProfile = S['ProviderProfile']
export type ProviderProfileCreate = S['ProviderProfileCreate']
export type ProviderProfileUpdate = S['ProviderProfileUpdate']
export type ProfileTestResult = S['ProfileTestResult']

export const PROFILES_KEY = ['provider-profiles'] as const

export const useProfiles = () => useQuery({ queryKey: PROFILES_KEY, queryFn: () => unwrap(api.GET('/provider-profiles')) })

export const useProfileModels = (id: string | null) =>
  useQuery({
    queryKey: ['provider-models', id],
    enabled: id !== null,
    queryFn: () => unwrap(api.GET('/provider-profiles/{profile_id}/models', { params: { path: { profile_id: id! } } })),
    staleTime: 5 * 60_000,
  })

/**
 * The only two calls that carry a key. They are plain functions, not useMutation, because the mutation cache keeps
 * its variables. A server message is shown with the key cut out, in case a faulty backend echoes it.
 */
async function withKey<T>(apiKey: string | undefined, call: () => Promise<T>): Promise<T> {
  try {
    return await call()
  } catch (e) {
    if (e instanceof ApiError && apiKey) throw new ApiError(e.status, e.code, e.message.split(apiKey).join('[key]'), e.fields.map((f) => ({ ...f, message: f.message.split(apiKey).join('[key]') })))
    throw e
  }
}

export const createProfile = (body: ProviderProfileCreate) =>
  withKey(body.api_key, () => unwrap(api.POST('/provider-profiles', { body })))

export const updateProfile = (id: string, body: ProviderProfileUpdate) =>
  withKey(body.api_key, () => unwrap(api.PATCH('/provider-profiles/{profile_id}', { params: { path: { profile_id: id } }, body })))

export const deleteProfile = (id: string) => unwrap(api.DELETE('/provider-profiles/{profile_id}', { params: { path: { profile_id: id } } }))

export const testProfile = (id: string, model?: string) =>
  unwrap(api.POST('/provider-profiles/{profile_id}/test', { params: { path: { profile_id: id } }, body: model ? { model } : {} }))
