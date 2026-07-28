import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LogoLockup } from './Logo'

describe('LogoLockup', () => {
  it('renders the Muhafiz wordmark', () => {
    render(<LogoLockup />)
    expect(screen.getByText('Muhafiz')).toBeInTheDocument()
  })
})
