import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// Without this, render() output from one test in a file stacks on top of
// the next test's, since jsdom's document isn't reset between tests.
afterEach(() => {
  cleanup()
})
