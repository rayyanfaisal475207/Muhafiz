import { Navigate, Outlet } from 'react-router-dom';
import { useAuthStore } from '../../store/authStore';
import { useEffect } from 'react';

export function ProtectedRoute() {
  const { isAuthenticated, isLoading } = useAuthStore();

  useEffect(() => {
    // We only need to trigger checkAuth if it's the first load and we don't know the status.
    // Actually, it's safer to have App.tsx trigger checkAuth globally so it loads before rendering routes.
    // Assuming App.tsx handles checkAuth on mount, we just rely on isLoading.
  }, []);

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
