// One subprocess runner for both bound directories (kernel and knowledge).
//
// Differences from a naive spawn: a deadline, caller cancellation, an output
// cap, and a stderr tail on every failure path. Non-zero exit is returned to the
// caller rather than thrown, because the kernel and the knowledge bridge both
// print structured JSON on some failures; only an unusable transport rejects.
//
// Killing has to reach the whole process group. `sctda run` spawns R (and R may
// spawn its own children); killing only the Python process would leave an R
// batch alive that keeps writing into `artifacts/<protocol>/<task>/` while the
// next attempt moves those same files aside. On POSIX the child therefore gets
// its own process group (`detached: true`) and a kill signals `-pid`; on Windows
// `taskkill /T` walks the tree instead, because negative pids are not a thing
// there. If the group is already gone, the direct kill is the fallback.

import { spawn } from 'node:child_process'

import { ProcessFailure } from './errors.js'

const DEFAULT_MAX_STDOUT_BYTES = 32 * 1024 * 1024
const DEFAULT_MAX_STDERR_BYTES = 256 * 1024
const WINDOWS = process.platform === 'win32'

/**
 * Every child group still running. Detaching a child from this process's group
 * also detaches it from the process's terminal signals, so a graceful exit must
 * reap them explicitly instead of leaving kernel and R batches behind.
 * @type {Map<number, import('node:child_process').ChildProcess>}
 */
const liveChildren = new Map()

/** Kill every tracked child group. Synchronous: safe from an exit handler. */
export function killLiveChildren() {
  for (const child of [...liveChildren.values()]) killTree(child)
  liveChildren.clear()
}

// Detaching puts a child in its own group, which also removes it from the
// terminal's process-group delivery: on shutdown this process must reap them
// explicitly. `exit` covers a normal return. Signals need care: merely adding a
// listener disables Node's default termination, so each handler reaps, then
// removes itself and re-raises the signal unless another listener (the host's
// own graceful shutdown) is still installed.
process.once('exit', () => {
  killLiveChildren()
})
for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP']) {
  const handler = () => {
    killLiveChildren()
    process.removeListener(signal, handler)
    if (process.listenerCount(signal) === 0) {
      try {
        process.kill(process.pid, signal)
      } catch {
        // Already terminating; the exit handler has done its part.
      }
    }
  }
  process.on(signal, handler)
}

/**
 * Kill one spawned child and every descendant it created.
 *
 * Exported because the runner is the only place that owns a `ChildProcess` with
 * a known group, and a test needs to prove the grandchild dies too.
 * @param {import('node:child_process').ChildProcess} child
 * @param {{platform?: NodeJS.Platform}} options
 * @returns {'group'|'tree'|'child'|'none'} which strategy actually ran
 */
export function killTree(child, { platform = process.platform } = {}) {
  const pid = child?.pid
  if (typeof pid !== 'number' || pid <= 0) return 'none'
  if (platform === 'win32') {
    try {
      const killer = spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' })
      killer.on('error', () => {})
      return 'tree'
    } catch {
      // Fall through to the direct kill.
    }
  } else {
    try {
      // Negative pid signals the whole group the child leads.
      process.kill(-pid, 'SIGKILL')
      return 'group'
    } catch {
      // ESRCH when the leader already exited; a direct kill may still work.
    }
  }
  try {
    child.kill('SIGKILL')
    return 'child'
  } catch {
    return 'none'
  }
}

/**
 * Run one command to completion.
 * @param {string} command
 * @param {string[]} args
 * @param {{cwd?: string, env?: Record<string, string>, timeoutMs?: number,
 *          signal?: AbortSignal, stdin?: string, maxStdoutBytes?: number,
 *          maxStderrBytes?: number, detached?: boolean}} options
 * @returns {Promise<{code: number|null, signal: string|null, stdout: string, stderr: string}>}
 * @throws {ProcessFailure} spawn error, deadline, cancellation, or output overflow
 */
export function runProcess(command, args, options = {}) {
  const {
    cwd,
    env,
    timeoutMs = 0,
    signal,
    stdin,
    maxStdoutBytes = DEFAULT_MAX_STDOUT_BYTES,
    maxStderrBytes = DEFAULT_MAX_STDERR_BYTES,
    // Own process group on POSIX so a timeout can reach R and its children.
    // Windows has no negative-pid group signal; `killTree` uses taskkill /T.
    detached = !WINDOWS,
  } = options

  return new Promise((resolve, reject) => {
    let child
    try {
      child = spawn(command, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'], detached })
    } catch (error) {
      reject(new ProcessFailure(`${command}: could not start (${error.message})`))
      return
    }
    if (detached && typeof child.pid === 'number') liveChildren.set(child.pid, child)
    child.once('close', () => {
      if (typeof child.pid === 'number') liveChildren.delete(child.pid)
    })

    let stdout = ''
    let stderr = ''
    let settled = false
    let timer = null
    let overflow = null
    let killStrategy = null

    const settle = (fn, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (signal) signal.removeEventListener('abort', onAbort)
      fn(value)
    }
    const kill = () => {
      if (killStrategy !== null && killStrategy !== 'none') return
      killStrategy = killTree(child)
    }
    const onAbort = () => {
      kill()
      settle(reject, new ProcessFailure(`${command}: cancelled by the caller; the child process tree was killed`, {
        kind: 'aborted',
        stdout,
        stderr,
      }))
    }

    if (signal) {
      if (signal.aborted) {
        onAbort()
        return
      }
      signal.addEventListener('abort', onAbort, { once: true })
    }
    if (timeoutMs > 0) {
      timer = setTimeout(() => {
        // Kill the group BEFORE settling: the caller's next attempt may start
        // immediately, and a still-breathing R batch would race its artifacts.
        kill()
        settle(reject, new ProcessFailure(
          `${command}: exceeded ${timeoutMs} ms; the child process tree was killed`,
          { kind: 'timeout', stdout, stderr },
        ))
      }, timeoutMs)
    }

    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      if (overflow) return
      if (stdout.length > maxStdoutBytes) {
        overflow = `stdout exceeded ${maxStdoutBytes} bytes`
        kill()
      }
    })
    child.stderr.on('data', (chunk) => {
      // stderr is diagnostic only: keep a bounded tail, never grow unbounded.
      stderr = (stderr + chunk).slice(-maxStderrBytes)
    })
    child.on('error', (error) => {
      settle(reject, new ProcessFailure(`${command}: process error (${error.message})`, {
        kind: 'spawn',
        stderr,
      }))
    })
    child.on('close', (code, closeSignal) => {
      if (overflow) {
        settle(reject, new ProcessFailure(`${command}: ${overflow}; the child process tree was killed`, {
          kind: 'overflow',
          exitCode: code,
          stdout,
          stderr,
        }))
        return
      }
      settle(resolve, { code, signal: closeSignal, stdout, stderr })
    })

    try {
      child.stdin.end(stdin ?? '')
    } catch {
      // A child that exited before reading stdin is reported by `close`.
    }
  })
}
