import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

const BUNDLE = fileURLToPath(new URL('..', import.meta.url))
const text = readFileSync(new URL('../cordis.patch.yml', import.meta.url), 'utf8')
const lines = text.split('\n')

test('the bundle inserts no root scope row; the plugin loads only inside presets', () => {
  // A root row would be four-space indented under the insert list. Preset
  // plugin rows are nested deeper and are scoped to their preset.
  const rootRows = lines.filter((line) => /^\s{4}- id: scope$/.test(line))
  assert.deepEqual(rootRows, [])
  const pluginRows = lines.filter((line) => line.trim() === 'name: dsh-plugin-scope')
  assert.equal(pluginRows.length, 5)
})

test('the lead preset exposes only the four closed worker presets', () => {
  assert.match(text, /agentPreset: scope-worker/)
  for (const preset of ['scope-worker', 'scope-literature', 'scope-data', 'scope-methods']) {
    assert.match(text, new RegExp(`- ${preset}`))
  }
  assert.equal((text.match(/toolName: subagent/g) ?? []).length >= 1, true)
  assert.match(text, /maxDepth: 1/)
  assert.match(text, /modelSelection: false/)
})

test('every worker preset is shell-free and file-write-free, and none composes a delegation tool', () => {
  const presetSections = text.split('- id: preset-scope-').filter((section, index) => index > 0)
  assert.equal(presetSections.length, 5)
  for (const section of presetSections) {
    const name = section.slice(0, section.indexOf('\n')).trim()
    if (name === 'lead') continue
    assert.doesNotMatch(section, /name: '@deepseek-ai\/dsh-tool-bash'/, `${name} composes bash`)
    assert.doesNotMatch(section, /name: '@deepseek-ai\/dsh-tool-subagent'/, `${name} composes delegation`)
    assert.match(section, /write: false/)
    assert.match(section, /edit: false/)
    assert.match(section, /applyPatch: false/)
    assert.match(section, /role: worker/)
  }
})

test('the default-preset overlay only selects, it does not replace the deployment default', () => {
  const overlay = readFileSync(new URL('../presets/scope-default.patch.yml', import.meta.url), 'utf8')
  assert.match(overlay, /default: standard/)
  assert.match(overlay, /selectedDefault: scope-lead/)
})

test('the shipped presets actually set the role the plugin now requires explicitly', () => {
  // The plugin default is `worker`; every shipped preset must name its role, or a
  // future edit that drops one silently narrows the lead to the read-only set.
  const pluginRows = text.split('name: dsh-plugin-scope').slice(1)
  assert.equal(pluginRows.length, 5)
  for (const row of pluginRows) {
    const tail = row.slice(0, row.indexOf('- id: preset-') === -1 ? row.length : row.indexOf('- id: preset-'))
    assert.match(tail, /role: (lead|worker)/, `a preset row is missing an explicit role:\n${tail.slice(0, 200)}`)
  }
})

test('preset delegation presets match the plugin closed set used by the guard', async () => {
  const { normalizeConfig } = await import('../lib/config.js')
  const presets = [...text.matchAll(/allowedPresets:\n((?:\s+- scope-[\w-]+\n)+)/g)]
  assert.ok(presets.length >= 1)
  for (const [, block] of presets) {
    const ids = block.split('\n').map((line) => line.trim().replace(/^- /, '')).filter(Boolean)
    const cfg = normalizeConfig({ role: 'lead', delegation: { allowedPresets: ids, maxDepth: 1 } }, BUNDLE)
    assert.deepEqual(cfg.delegation.allowedPresets, ids)
    assert.equal(cfg.delegation.maxDepth, 1)
  }
})
