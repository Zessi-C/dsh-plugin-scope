import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { ProcessFailure } from '../lib/errors.js'
import { killTree, runProcess } from '../lib/proc.js'

const POSIX = process.platform !== 'win32'
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * A Tiny process that spawns a grandchild which keeps writing to a file.
 *
 * This reproduces the real shape: `sctda run` spawns Rscript, R writes into
 * `artifacts/<protocol>/<task>/`. Killing only the direct child leaves the
 * grandchild writing, which is the defect being tested.
 * @param {string} dir
 * @returns {{script: string, pids: string, output: string}}
 */
function treeScript(dir) {
  const pids = join(dir, 'pids.txt')
  const output = join(dir, 'writes.log')
  const script = join(dir, 'parent.mjs')
  writeFileSync(script, `
    import { spawn } from 'node:child_process'
    import { writeFileSync } from 'node:fs'
    const child = spawn(process.execPath, ['-e', ${JSON.stringify(`
      const { appendFileSync } = require('node:fs')
      const target = ${JSON.stringify(output)}
      const timer = setInterval(() => appendFileSync(target, 'x'), 100)
      setTimeout(() => { clearInterval(timer); process.exit(0) }, 60000)
    `)}], { stdio: 'ignore' })
    writeFileSync(${JSON.stringify(pids)}, String(child.pid))
    const timer = setInterval(() => {}, 1000)
    setTimeout(() => { clearInterval(timer) }, 60000)
  `)
  return { script, pids, output }
}

const alive = (pid) => {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

test('a timeout kills the whole process group, not just the direct child', { skip: !POSIX }, async () => {
  const dir = mkdtempSync(join(tmpdir(), 'scope-proc-'))
  try {
    const { script, pids, output } = treeScript(dir)
    await assert.rejects(
      runProcess(process.execPath, [script], { timeoutMs: 600 }),
      (error) => {
        assert.ok(error instanceof ProcessFailure)
        assert.equal(error.kind, 'timeout')
        assert.match(error.message, /process tree was killed/)
        return true
      },
    )
    // The grandchild must be gone: it was writing every 100 ms.
    await sleep(500)
    const before = existsSync(output) ? readFileSync(output, 'utf8').length : 0
    await sleep(700)
    const after = existsSync(output) ? readFileSync(output, 'utf8').length : 0
    assert.equal(after, before, `the grandchild kept writing after the timeout (${before} -> ${after} bytes)`)
    if (existsSync(pids)) {
      const grandchild = Number(readFileSync(pids, 'utf8').trim())
      assert.equal(alive(grandchild), false, `grandchild ${grandchild} survived the timeout`)
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('cancellation through the caller signal reaps the group too', { skip: !POSIX }, async () => {
  const dir = mkdtempSync(join(tmpdir(), 'scope-proc-'))
  try {
    const { script, output } = treeScript(dir)
    const controller = new AbortController()
    const running = runProcess(process.execPath, [script], { signal: controller.signal })
    await sleep(300)
    controller.abort()
    await assert.rejects(running, (error) => {
      assert.equal(error.kind, 'aborted')
      assert.match(error.message, /process tree was killed/)
      return true
    })
    await sleep(400)
    const before = existsSync(output) ? readFileSync(output, 'utf8').length : 0
    await sleep(600)
    const after = existsSync(output) ? readFileSync(output, 'utf8').length : 0
    assert.equal(after, before, 'the grandchild survived cancellation')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('a normal exit is not disturbed by the group bookkeeping', async () => {
  const result = await runProcess(process.execPath, ['-e', 'process.stdout.write("done")'], {
    timeoutMs: 10000,
  })
  assert.equal(result.code, 0)
  assert.equal(result.stdout, 'done')
})

test('killTree falls back when the leader has already exited', () => {
  const fakeChild = { pid: 999999999, kill: () => true }
  const strategy = killTree(fakeChild)
  assert.ok(['group', 'child', 'none'].includes(strategy))
})

test('a SIGTERM to the host reaps detached children instead of orphaning them', { skip: !POSIX }, async () => {
  const dir = mkdtempSync(join(tmpdir(), 'scope-proc-'))
  try {
    const { script, output, pids } = treeScript(dir)
    // A host that runs a long command through the runner and is then terminated.
    // Without explicit reaping the detached group survives the host.
    const host = join(dir, 'host.mjs')
    writeFileSync(host, `
      import { runProcess } from ${JSON.stringify(new URL('../lib/proc.js', import.meta.url).href)}
      await runProcess(process.execPath, [${JSON.stringify(script)}], { timeoutMs: 60000 }).catch(() => {})
    `)
    const proc = spawnHost(host)
    await sleep(900)
    proc.kill('SIGTERM')
    await sleep(700)
    const before = existsSync(output) ? readFileSync(output, 'utf8').length : 0
    await sleep(800)
    const after = existsSync(output) ? readFileSync(output, 'utf8').length : 0
    assert.equal(after, before, 'a detached grandchild kept writing after the host was terminated')
    if (existsSync(pids)) {
      const grandchild = Number(readFileSync(pids, 'utf8').trim())
      assert.equal(alive(grandchild), false, `grandchild ${grandchild} survived the host SIGTERM`)
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

function spawnHost(script) {
  return spawn(process.execPath, [script], { stdio: 'ignore', detached: true })
}
