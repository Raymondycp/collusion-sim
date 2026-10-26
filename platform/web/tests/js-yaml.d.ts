// js-yaml 4 ships no types; the contract test uses only `load`.
declare module 'js-yaml' {
  export function load(text: string): unknown
}
