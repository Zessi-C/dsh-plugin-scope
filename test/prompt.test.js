import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { createPromptSource, promptVersion } from '../lib/prompt.js'

function promptDir(text = '**版本** v1.1（2026-11-13）。\n正文。\n') {
  const dir = mkdtempSync(join(tmpdir(), 'scope-prompt-'))
  const path = join(dir, 'SCOPE_PROMPT.md')
  writeFileSync(path, text)
  return { dir, path }
}

test('promptVersion reads the version marker', () => {
  assert.equal(promptVersion('**版本** v1.1（2026-11-13）。'), 'v1.1')
  assert.equal(promptVersion('Version: 2.0'), '2.0')
  assert.equal(promptVersion('no marker here'), null)
})

test('prompt source hashes the text and reloads edits in place', () => {
  const { path } = promptDir()
  const source = createPromptSource({ path })
  const first = source.read()
  assert.equal(first.version, 'v1.1')
  assert.match(first.sha256, /^[0-9a-f]{64}$/)
  assert.equal(source.read().sha256, first.sha256)

  writeFileSync(path, '**版本** v1.2\n新增一行。\n')
  const second = source.read()
  assert.notEqual(second.sha256, first.sha256)
  assert.equal(second.version, 'v1.2')
  assert.match(second.text, /新增一行/)
  assert.equal(source.state().last_error, null)
})

test('an unreadable required prompt after the first read keeps the last good text and records the error', () => {
  const { dir, path } = promptDir()
  const warnings = []
  const source = createPromptSource({ path, logger: { warn: (message) => warnings.push(message) } })
  const good = source.read()
  rmSync(dir, { recursive: true, force: true })

  const degraded = source.read()
  assert.equal(degraded.text, good.text)
  assert.equal(degraded.sha256, good.sha256)
  assert.match(degraded.last_error, /ENOENT/)
  assert.equal(source.state().last_error !== null, true)
  assert.equal(warnings.length, 1)
  // A second failure is not re-warned on every assembly.
  source.read()
  assert.equal(warnings.length, 1)
})

test('a required prompt that cannot be read at load fails instead of injecting nothing', () => {
  const dir = mkdtempSync(join(tmpdir(), 'scope-missing-'))
  const path = join(dir, 'absent.md')
  const source = createPromptSource({ path })
  assert.throws(() => source.read(), /unreadable/)
  assert.equal(source.state().loaded, false)
})

test('an optional prompt that is missing returns an absent state instead of throwing', () => {
  const dir = mkdtempSync(join(tmpdir(), 'scope-optional-'))
  const path = join(dir, 'absent.md')
  const source = createPromptSource({ path, required: false })
  const state = source.read()
  assert.equal(state.loaded, false)
  assert.equal(state.absent, true)
  assert.equal(state.text, '')
  assert.equal(source.state().absent, true)
})

test('an optional prompt that appears later is picked up on the next read', () => {
  const dir = mkdtempSync(join(tmpdir(), 'scope-optional-'))
  const path = join(dir, 'later.md')
  const source = createPromptSource({ path, required: false })
  assert.equal(source.read().loaded, false)
  writeFileSync(path, '**版本** v2.0\n后来出现的草案。\n')
  const loaded = source.read()
  assert.equal(loaded.loaded, true)
  assert.equal(loaded.version, 'v2.0')
  assert.match(loaded.text, /后来出现/)
})

test('an empty prompt file is refused when it is required and reported absent when optional', () => {
  const { path } = promptDir('   \n')
  assert.throws(() => createPromptSource({ path }).read(), /empty/)

  const optional = createPromptSource({ path, required: false })
  const state = optional.read()
  assert.equal(state.loaded, false)
  assert.equal(state.absent, true)
  assert.match(state.last_error, /empty/)
})

test('a prompt directory path is refused rather than read as text', () => {
  const dir = mkdtempSync(join(tmpdir(), 'scope-dir-'))
  const nested = join(dir, 'nested')
  mkdirSync(nested)
  const source = createPromptSource({ path: nested })
  assert.throws(() => source.read())
})
