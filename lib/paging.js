// Offset pagination for plugin-owned listings.
//
// Deliberately NOT applied to the kernel's candidate/analysis tools: the
// science policy requires a complete candidate enumeration, and a page-limited
// view is exactly the hidden pre-filter that policy forbids. Pagination here is
// for capability listings and for the knowledge API, which is built for it.

import { PageError } from './errors.js'

/**
 * Validate and describe one page.
 * @param {{limit?: number, offset?: number, defaultLimit?: number,
 *          maxLimit?: number, total?: number}} request
 * @returns {{offset: number, limit: number, has_more: boolean,
 *            next_offset: number|null, total?: number, returned?: number}}
 * @throws {PageError} non-integer, negative, or oversized request
 */
export function resolvePage(request = {}) {
  const { limit, offset, defaultLimit = 50, maxLimit = 200, total } = request
  const size = limit === undefined || limit === null ? defaultLimit : limit
  const start = offset === undefined || offset === null ? 0 : offset
  if (!Number.isInteger(size) || size < 1) {
    throw new PageError(`limit must be a positive integer (got ${JSON.stringify(limit)})`)
  }
  if (size > maxLimit) {
    throw new PageError(
      `limit ${size} exceeds the maximum ${maxLimit}; page with limit + offset instead of requesting everything at once`,
    )
  }
  if (!Number.isInteger(start) || start < 0) {
    throw new PageError(`offset must be a non-negative integer (got ${JSON.stringify(offset)})`)
  }
  const page = { offset: start, limit: size, has_more: false, next_offset: null }
  if (Number.isInteger(total) && total >= 0) {
    const returned = Math.max(0, Math.min(size, total - start))
    page.total = total
    page.returned = returned
    page.has_more = start + returned < total
    page.next_offset = page.has_more ? start + size : null
  }
  return page
}

/**
 * Slice a list with a page already validated by {@link resolvePage}.
 * @template T @param {T[]} items @param {{offset: number, limit: number}} page
 * @returns {T[]}
 */
export function paginate(items, page) {
  return items.slice(page.offset, page.offset + page.limit)
}
