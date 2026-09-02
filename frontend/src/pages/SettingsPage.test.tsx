import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { SettingsPage } from './SettingsPage'
import { useAuthStore } from '../store/authStore'
import { useProfileStore } from '../store/profileStore'
import type { UserContextProfile } from '../store/profileStore'

// Module 7 (FRONTEND_UX_MATURITY_IMPLEMENTATION_PLAN.md): SettingsPage's
// single page-level "Settings saved successfully!" banner became per-field
// inline confirmation, and its three fields moved into an icon+label+
// description+control row template. These guard both the existing
// load/submit/error behavior (must not regress) and the new per-field
// confirmation logic (must show only on fields that actually changed).

const baseProfile: UserContextProfile = {
  context_text: 'Duty officer at the Aabpara station.',
  preferred_language: 'auto',
  llm_mode: 'cloud',
}

function renderSettings() {
  return render(
    <MemoryRouter>
      <SettingsPage />
    </MemoryRouter>,
  )
}

function setup(profileOverrides: Partial<UserContextProfile> = {}) {
  useAuthStore.setState({ user: { id: 'u1', email: 'officer@muhafiz.pk', role: 'investigator', is_admin: false } as any })
  useProfileStore.setState({
    profile: { ...baseProfile, ...profileOverrides },
    isLoading: false,
    error: null,
  })
  vi.spyOn(useProfileStore.getState(), 'loadProfile').mockResolvedValue(undefined)
}

describe('SettingsPage — existing behavior (must not regress)', () => {
  beforeEach(() => setup())

  it('loads and displays the profile fields', () => {
    renderSettings()
    expect(screen.getByLabelText('Context')).toHaveValue(baseProfile.context_text)
    expect(screen.getByLabelText('Preferred Language')).toHaveValue('auto')
    expect(screen.getByLabelText('AI Model Mode')).toHaveValue('cloud')
  })

  it('submits the current field values to updateProfile', async () => {
    const updateProfile = vi.fn().mockResolvedValue(undefined)
    useProfileStore.setState({ updateProfile })
    renderSettings()

    await userEvent.click(screen.getByRole('button', { name: /Save Changes/i }))
    expect(updateProfile).toHaveBeenCalledWith(baseProfile)
  })

  it('shows the store error message', () => {
    useProfileStore.setState({ error: 'Failed to update profile' })
    renderSettings()
    expect(screen.getByText('Failed to update profile')).toBeInTheDocument()
  })

  it('disables Save Changes and shows "Saving..." while a submit is in flight', () => {
    useProfileStore.setState({ isLoading: true })
    renderSettings()
    const btn = screen.getByRole('button', { name: /Saving/i })
    expect(btn).toBeDisabled()
  })
})

describe('SettingsPage — per-field save confirmation', () => {
  beforeEach(() => setup())

  it('shows a "Saved" badge only on the field that actually changed', async () => {
    const updateProfile = vi.fn().mockImplementation(async (data: UserContextProfile) => {
      useProfileStore.setState({ profile: data })
    })
    useProfileStore.setState({ updateProfile })
    renderSettings()

    await userEvent.selectOptions(screen.getByLabelText('Preferred Language'), 'english')
    await userEvent.click(screen.getByRole('button', { name: /Save Changes/i }))

    await waitFor(() => {
      expect(screen.getAllByText('Saved')).toHaveLength(1)
    })
    const languageRow = screen.getByTestId('settings-row-preferred_language')
    expect(within(languageRow).getByText('Saved')).toBeInTheDocument()
    expect(within(screen.getByTestId('settings-row-context_text')).queryByText('Saved')).not.toBeInTheDocument()
    expect(within(screen.getByTestId('settings-row-llm_mode')).queryByText('Saved')).not.toBeInTheDocument()
  })

  it('shows no confirmation on any field when nothing actually changed', async () => {
    const updateProfile = vi.fn().mockImplementation(async (data: UserContextProfile) => {
      useProfileStore.setState({ profile: data })
    })
    useProfileStore.setState({ updateProfile })
    renderSettings()

    await userEvent.click(screen.getByRole('button', { name: /Save Changes/i }))

    await waitFor(() => expect(updateProfile).toHaveBeenCalled())
    expect(screen.queryByText('Saved')).not.toBeInTheDocument()
  })

  it('shows confirmation on every field that changed when more than one did', async () => {
    const updateProfile = vi.fn().mockImplementation(async (data: UserContextProfile) => {
      useProfileStore.setState({ profile: data })
    })
    useProfileStore.setState({ updateProfile })
    renderSettings()

    await userEvent.selectOptions(screen.getByLabelText('Preferred Language'), 'urdu')
    await userEvent.selectOptions(screen.getByLabelText('AI Model Mode'), 'local')
    await userEvent.click(screen.getByRole('button', { name: /Save Changes/i }))

    await waitFor(() => expect(screen.getAllByText('Saved')).toHaveLength(2))
    expect(within(screen.getByTestId('settings-row-preferred_language')).getByText('Saved')).toBeInTheDocument()
    expect(within(screen.getByTestId('settings-row-llm_mode')).getByText('Saved')).toBeInTheDocument()
  })

  it('clears a field\'s confirmation as soon as that field is edited again', async () => {
    const updateProfile = vi.fn().mockImplementation(async (data: UserContextProfile) => {
      useProfileStore.setState({ profile: data })
    })
    useProfileStore.setState({ updateProfile })
    renderSettings()

    await userEvent.selectOptions(screen.getByLabelText('Preferred Language'), 'english')
    await userEvent.click(screen.getByRole('button', { name: /Save Changes/i }))
    await waitFor(() => expect(screen.getAllByText('Saved')).toHaveLength(1))

    await userEvent.selectOptions(screen.getByLabelText('Preferred Language'), 'urdu')
    expect(screen.queryByText('Saved')).not.toBeInTheDocument()
  })
})
