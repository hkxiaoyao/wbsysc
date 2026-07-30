import assert from 'node:assert/strict'
import test from 'node:test'

import api, { subscribeAdminSessionExpired } from '../api.js'

test('an admin API 401 clears the token and notifies the shell to show login', async () => {
  const removed = []
  const listeners = new Map()
  const storage = {
    getItem: () => 'stale-admin-token',
    removeItem: key => removed.push(key),
  }
  const eventTarget = {
    Event,
    addEventListener: (type, listener) => listeners.set(type, listener),
    removeEventListener: (type, listener) => {
      if (listeners.get(type) === listener) listeners.delete(type)
    },
    dispatchEvent: event => listeners.get(event.type)?.(event),
  }
  let expired = 0
  const unsubscribe = subscribeAdminSessionExpired(() => { expired += 1 }, eventTarget)
  const originalAdapter = api.defaults.adapter
  const originalStorage = globalThis.localStorage
  const originalWindow = globalThis.window
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true })
  Object.defineProperty(globalThis, 'window', { value: eventTarget, configurable: true })
  api.defaults.adapter = async config => {
    const error = new Error('Request failed with status code 401')
    error.config = config
    error.response = { status: 401, data: { detail: '未登录或会话过期' } }
    throw error
  }

  try {
    await assert.rejects(api.get('/admin/session'), /401/)
  } finally {
    api.defaults.adapter = originalAdapter
    unsubscribe()
    Object.defineProperty(globalThis, 'localStorage', { value: originalStorage, configurable: true })
    Object.defineProperty(globalThis, 'window', { value: originalWindow, configurable: true })
  }

  assert.deepEqual(removed, ['wbg_admin_token'])
  assert.equal(expired, 1)
  assert.equal(listeners.size, 0)
})
