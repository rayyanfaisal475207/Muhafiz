import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { RegisterPage } from './RegisterPage'
import { useAuthStore } from '../store/authStore'

// Audit hypothesis #1: the password input used to say minLength={8} while
// the backend (routes.py's UserCreate.password validator) has always
// required 12+ -- a user could pass this client-side check, then get a
// confusing 422 on submit. These guard the fix: the input's own
// constraint must match the real backend minimum.
function renderRegister() {
  return render(
    <MemoryRouter>
      <RegisterPage />
    </MemoryRouter>,
  )
}

describe('RegisterPage — password length matches the backend minimum', () => {
  beforeEach(() => {
    useAuthStore.setState({ isAuthenticated: false, error: null, isLoading: false })
  })

  it('sets minLength to 12 on the password input, not 8', () => {
    renderRegister()
    const password = screen.getByLabelText('Password') as HTMLInputElement
    expect(password.minLength).toBe(12)
  })

  it('shows a 12-character helper hint', () => {
    renderRegister()
    expect(screen.getByText(/at least 12 characters/i)).toBeInTheDocument()
  })
})
