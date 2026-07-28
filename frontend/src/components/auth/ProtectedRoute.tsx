import { Navigate, Outlet } from 'react-router-dom';
import { useAuthStore } from '../../store/authStore';

export function ProtectedRoute() {
  const { isAuthenticated, isLoading } = useAuthStore();

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center" style={{ background: 'var(--bg-base)' }}>
        <div className="flex flex-col items-center">
          <div className="h-8 w-8 animate-spin rounded-pill border-[3px] border-t-transparent" style={{ borderColor: 'var(--accent)', borderTopColor: 'transparent' }}></div>
          <p className="mt-4 text-[var(--text-muted)]">Loading session...</p>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <Outlet />;
}
