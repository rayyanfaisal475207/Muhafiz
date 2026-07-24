// ============================================================
// App — Router + Layout
// ============================================================

import { BrowserRouter, Routes, Route, Navigate, useNavigate } from 'react-router-dom';
import { useEffect } from 'react';
import { AppLayout } from './components/layout/AppLayout';
import { ChatPage } from './pages/ChatPage';
import { SettingsPage } from './pages/SettingsPage';
import { LoginPage } from './pages/LoginPage';
import { RegisterPage } from './pages/RegisterPage';
import { ProtectedRoute } from './components/auth/ProtectedRoute';
import { useAuthStore } from './store/authStore';

function AppRoutes() {
  const { checkAuth, setUnauthenticated } = useAuthStore();
  const navigate = useNavigate();

  useEffect(() => {
    // Check session on app load
    (async () => {
      await checkAuth();
    })();

    // Global listener for 401 interceptors from api.ts
    const handleUnauthorized = () => {
      setUnauthenticated();
      const path = window.location.pathname;
      if (path !== '/login' && path !== '/register') {
        navigate('/login');
      }
    };


    window.addEventListener('auth:unauthorized', handleUnauthorized);
    return () => window.removeEventListener('auth:unauthorized', handleUnauthorized);
  }, [checkAuth, setUnauthenticated, navigate]);

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      
      <Route element={<ProtectedRoute />}>
        <Route element={<AppLayout />}>
          <Route path="/" element={<ChatPage />} />
          <Route path="/chat/:id" element={<ChatPage />} />
          {/* Knowledge-base ingestion moved to the admin app. Users attach
              files to a single conversation from the composer instead. */}
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  );
}

