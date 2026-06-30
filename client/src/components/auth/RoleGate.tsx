import { type ReactNode } from 'react';
import { Navigate } from 'react-router-dom';
import { ShieldOff } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import type { Role } from '@/config/constants';

interface RoleGateProps {
  minRole: Role;
  children: ReactNode;
}

/**
 * Renders children only when the current user satisfies `minRole`.
 * In OIDC mode, unauthorized users are redirected to the home page with a
 * query flag so the layout can surface a "permission denied" notice.
 * In open / api-key / auto mode every caller is admin, so the gate is always
 * open (preserving local-dev ergonomics).
 */
export function RoleGate({ minRole, children }: RoleGateProps) {
  const { mode, hasRole } = useAuth();

  // Non-OIDC modes grant admin to all callers — gate is always open.
  if (mode !== 'oidc') return <>{children}</>;

  if (hasRole(minRole)) return <>{children}</>;

  return (
    <div className="flex flex-col items-center justify-center gap-4 py-24 text-center">
      <ShieldOff className="size-10 text-muted-foreground" />
      <p className="text-sm font-medium text-foreground">
        You need the <span className="font-bold">{minRole}</span> role to access this page.
      </p>
      <Navigate to="/" replace />
    </div>
  );
}
