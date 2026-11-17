// Shared failure types for the SCOPE bundle.
//
// Every failure here is meant to reach the model as a failed call with the real
// cause attached. Nothing in this bundle turns a failure into an empty result.

export class ProcessFailure extends Error {
  constructor(message, details = {}) {
    super(message)
    this.name = 'ProcessFailure'
    this.kind = details.kind ?? 'spawn'
    this.exitCode = details.exitCode ?? null
    this.signal = details.signal ?? null
    this.stdout = details.stdout ?? ''
    this.stderr = details.stderr ?? ''
  }
}

export class ManifestError extends Error {
  constructor(message, details = {}) {
    super(message)
    this.name = 'ManifestError'
    this.stdout = details.stdout ?? ''
    this.stderr = details.stderr ?? ''
    this.exitCode = details.exitCode ?? null
  }
}

export class PageError extends Error {
  constructor(message) {
    super(message)
    this.name = 'PageError'
  }
}

/** Last `max` characters of a stream, trimmed, with an ellipsis when cut. */
export function tail(text, max = 1200) {
  const value = String(text ?? '').trim()
  if (value.length <= max) return value
  return `…${value.slice(-max)}`
}
