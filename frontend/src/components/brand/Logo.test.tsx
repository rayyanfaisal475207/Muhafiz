import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LogoLockup } from './Logo'

describe('LogoLockup', () => {
  it('renders the MorseAI wordmark', () => {
    render(<LogoLockup />)
    expect(screen.getByText('MorseAI')).toBeInTheDocument()
  })
})
