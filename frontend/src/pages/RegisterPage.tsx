import { useState, useEffect } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { useAuthStore } from '../store/authStore';
import { LogoLockup } from '../components/brand/Logo';

export function RegisterPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [companyName, setCompanyName] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const { register, error, clearError, isAuthenticated, isLoading } = useAuthStore();
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
    await register(email, password, companyName || undefined);
  };

  return (
    <div className="flex h-screen items-center justify-center px-6" style={{ background: 'var(--bg-base)' }}>
      <div className="w-full max-w-md bg-[var(--bg-surface)] p-8 shadow-[var(--shadow-lg)] rounded-lg border border-[var(--border)]">

        <div className="mb-7 flex flex-col items-center gap-3 text-center">
          <LogoLockup />
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            Create an Account
          </p>
        </div>

        {error && (
          <div className="mb-6 border-l-4 border-[var(--error)] bg-[var(--error-soft)] p-4">
            <p className="text-sm text-[var(--error)]">{error}</p>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <label htmlFor="register-email" className="block text-sm font-medium text-[var(--text-primary)]">Email Address</label>
            <input
              id="register-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              className="mt-1 block w-full rounded-sm border border-[var(--border-strong)] px-3 py-2 text-[var(--text-primary)] focus:border-[var(--accent)] focus:outline-none"
            />
          </div>

          <div>
            <label htmlFor="register-password" className="block text-sm font-medium text-[var(--text-primary)]">Password</label>
            {/* Audit hypothesis #1: this used to say minLength={8} while the
                backend (routes.py's UserCreate.password validator) has always
                required 12+ -- a user could pass this client-side check, then
                get a confusing 422 on submit. 12 matches the real backend
                minimum so the two can't disagree again. */}
            <p className="mt-1 text-xs text-[var(--text-muted)]">At least 12 characters.</p>
            <div className="relative mt-1">
              <input
                id="register-password"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={12}
                className="block w-full rounded-sm border border-[var(--border-strong)] px-3 py-2 text-[var(--text-primary)] focus:border-[var(--accent)] focus:outline-none pr-16"
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute inset-y-0 right-0 flex items-center px-3 text-sm text-[var(--text-muted)] hover:text-[var(--text-primary)]"
              >
                {showPassword ? "Hide" : "Show"}
              </button>
            </div>
          </div>

          <div>
            <label htmlFor="register-company" className="block text-sm font-medium text-[var(--text-primary)]">Company Name (Optional)</label>
            <input
              id="register-company"
              type="text"
              value={companyName}
              onChange={(e) => setCompanyName(e.target.value)}
              className="mt-1 block w-full rounded-sm border border-[var(--border-strong)] px-3 py-2 text-[var(--text-primary)] focus:border-[var(--accent)] focus:outline-none"
            />
          </div>

          <button
            type="submit"
            disabled={isLoading}
            className="btn-accent w-full py-2.5 text-sm"
          >
            {isLoading ? 'Registering...' : 'Register'}
          </button>
        </form>

        <div className="mt-6 text-center text-sm">
          <span className="text-[var(--text-muted)]">Already have an account? </span>
          <Link to="/login" className="font-semibold text-[var(--text-primary)] hover:text-[var(--accent)]">
            Sign in
          </Link>
        </div>
      </div>
    </div>
  );
}
