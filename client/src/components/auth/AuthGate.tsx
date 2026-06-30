import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';

/**
 * Layout-route auth guard.
 *
 * Used as `<Route element={<AuthGate />}>` in App.tsx so all nested routes
 * are protected.
 *
 * - While the auth config is loading, show a centred spinner.
 * - In OIDC mode, unauthenticated users are redirected to /login preserving
 *   the intended destination so they land back there after signing in.
 * - In every other mode (open / apikey / auto) the app is reachable as-is.
 */
export function AuthGate() {
  const { mode, loading, isAuthenticated } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="fixed inset-0 flex flex-col items-center justify-center gap-3 bg-background">
        <Loader2 className="size-8 animate-spin text-primary" />
        <p className="text-sm text-muted-foreground">Loading…</p>
      </div>
    );
  }

  if (mode === 'oidc' && !isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return <Outlet />;
}
