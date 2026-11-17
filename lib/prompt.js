// Prompt sources for the SCOPE bundle.
//
// Two different contracts share one loader:
//
//   * the short runtime policy ships with the package and is required. A read
//     failure before the first successful read refuses plugin load, because a
//     SCOPE agent running without its operating policy is worse than a plugin
//     that visibly failed to load;
//   * the long SCOPE draft the user asked to keep is optional. A missing file
//     is an `absent` state, not an error. If it appears later it is injected on
//     the next assembly.
//
// After the first successful read, a later failure keeps the last good text and
// records the error so the boundary tool can report a stale prompt instead of
// silently serving empty policy.

import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'

/** @param {string} text @returns {string|null} */
export function promptVersion(text) {
  const match = /(?:版本|version)\s*[:：]?\s*\**\s*(v?\d+(?:\.\d+)*)/i.exec(String(text ?? ''))
  return match ? match[1] : null
}

function absentState(path, lastError = null) {
  return {
    path,
    loaded: false,
    absent: true,
    text: '',
    sha256: null,
    bytes: 0,
    version: null,
    loaded_utc: null,
    last_error: lastError,
  }
}

/**
 * A reloadable prompt source.
 * @param {{path: string, required?: boolean, label?: string,
 *          logger?: {warn?: Function}}} options
 * @returns {{path: string, required: boolean, read: () => object, state: () => object}}
 */
export function createPromptSource({ path, required = true, label = 'prompt', logger } = {}) {
  let cache = null
  let lastError = null
  let warned = false

  function read() {
    if (typeof path !== 'string' || path.trim() === '') {
      if (!required) return absentState(path, null)
      throw new Error(`SCOPE ${label} path is empty`)
    }
    try {
      const text = readFileSync(path, 'utf8')
      if (text.trim() === '') throw new Error('file is empty or whitespace only')
      const sha256 = createHash('sha256').update(text).digest('hex')
      if (cache === null || cache.sha256 !== sha256) {
        cache = {
          path,
          loaded: true,
          absent: false,
          text,
          sha256,
          bytes: Buffer.byteLength(text),
          version: promptVersion(text),
          loaded_utc: new Date().toISOString(),
        }
        warned = false
      }
      lastError = null
      return cache
    } catch (error) {
      const detail = error.code ? `${error.code}: ${error.message}` : error.message
      lastError = detail
      if (cache === null) {
        if (!required) return absentState(path, detail)
        throw new Error(`SCOPE ${label} is unreadable at ${path} (${detail})`)
      }
      if (!required) {
        // Optional material that disappears must stop being injected; keeping
        // a stale draft would present withdrawn text as current.
        cache = null
        return absentState(path, detail)
      }
      if (!warned) {
        warned = true
        logger?.warn?.(`scope: ${label} reload failed (${detail}); keeping last good text ${cache.sha256.slice(0, 12)}`)
      }
      return { ...cache, last_error: detail }
    }
  }

  function state() {
    if (cache === null) {
      if (!required) return absentState(path, lastError)
      return { path, loaded: false, absent: false, last_error: lastError }
    }
    return {
      path,
      loaded: true,
      absent: false,
      sha256: cache.sha256,
      bytes: cache.bytes,
      version: cache.version,
      loaded_utc: cache.loaded_utc,
      last_error: lastError,
    }
  }

  return { path, required, read, state }
}
