import { useState, useEffect } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useAuthStore } from '../store/authStore';
import { LogoLockup } from '../components/brand/Logo';

export function LoginPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const { login, error, clearError, isAuthenticated, isLoading } = useAuthStore();
  const navigate = useNavigate();

  useEffect(() => {
    clearError();
  }, [clearError]);

  useEffect(() => {
    if (isAuthenticated) {
      navigate('/', { replace: true });
    }
  }, [isAuthenticated, navigate]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await login(email, password);
  };

  const fieldClass =
    'mt-1.5 block w-full rounded-sm px-3 py-2.5 text-[15px] bg-[var(--bg-surface)] text-[var(--text-primary)] border border-[var(--border-strong)] transition-colors placeholder-[var(--text-faint)] hover:border-[var(--border-hover)] focus:border-[var(--accent)] focus:outline-none';

  return (
    <div
      className="flex h-screen items-center justify-center px-6"
      style={{ background: 'var(--bg-base)' }}
    >
      <div
        className="w-full max-w-[400px] p-8 rounded-lg"
        style={{
          background: 'var(--bg-surface)',
          border: '1px solid var(--border)',
          boxShadow: 'var(--shadow-lg)',
        }}
      >
        <div className="mb-7 flex flex-col items-center gap-3 text-center">
          <LogoLockup />
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            Sign in to your workspace
          </p>
        </div>

        {error && (
          <div
            className="mb-5 px-3.5 py-2.5 rounded-sm text-sm"
            style={{
              background: 'var(--error-soft)',
              border: '1px solid color-mix(in srgb, var(--error) 30%, transparent)',
              color: 'var(--text-secondary)',
            }}
            role="alert"
          >
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-5">
          <div>
            <label
              className="block text-[13px] font-medium"
              style={{ color: 'var(--text-secondary)' }}
            >
              Email address
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
              className={fieldClass}
            />
          </div>

          <div>
            <label
              className="block text-[13px] font-medium"
              style={{ color: 'var(--text-secondary)' }}
            >
              Password
            </label>
            <div className="relative">
              <input
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="current-password"
                className={`${fieldClass} pr-16`}
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute inset-y-0 right-0 flex items-center px-3 text-[13px] transition-colors"
                style={{ color: 'var(--text-muted)' }}
              >
                {showPassword ? 'Hide' : 'Show'}
              </button>
            </div>
          </div>

          <button type="submit" disabled={isLoading} className="btn-accent w-full py-2.5 text-sm">
            {isLoading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <div className="mt-6 text-center text-sm">
          <span style={{ color: 'var(--text-muted)' }}>Don't have an account? </span>
          <Link
            to="/register"
            className="font-medium hover:underline underline-offset-2"
            style={{ color: 'var(--accent)' }}
          >
            Create one
          </Link>
        </div>
      </div>
    </div>
  );
}
