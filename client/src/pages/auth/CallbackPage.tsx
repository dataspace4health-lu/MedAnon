/**
 * /auth/callback, PKCE code exchange handler.
 *
 * Keycloak redirects here with ?code=...&state=... after the user authenticates.
 * We hand off to oidc-client-ts which verifies the state, exchanges the code
 * for tokens, and returns the path the user originally wanted.
 */

import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ShieldCheck, Loader2, AlertCircle } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import { Button } from '@/components/ui/button';

export default function CallbackPage() {
  const { handleCallback } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    handleCallback()
      .then((returnPath) => {
        navigate(returnPath, { replace: true });
      })
      .catch((err: unknown) => {
        const msg =
          err instanceof Error ? err.message : 'Authentication failed.';
        setError(msg);
      });
    // Run once, the URL params are consumed by the first call.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-4">
        <div className="w-full max-w-sm rounded-2xl border border-border bg-card p-8 text-center shadow-sm">
          <AlertCircle className="mx-auto mb-4 size-10 text-destructive" />
          <h2 className="mb-2 text-base font-semibold text-foreground">
            Sign-in failed
          </h2>
          <p className="mb-6 text-sm text-muted-foreground">{error}</p>
          <Button
            variant="outline"
            className="w-full"
            onClick={() => navigate('/login', { replace: true })}
          >
            Back to sign in
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="flex flex-col items-center gap-3 text-center">
        <span className="flex size-12 items-center justify-center rounded-xl bg-primary text-primary-foreground">
          <ShieldCheck className="size-6" />
        </span>
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
        <p className="text-sm text-muted-foreground">Completing sign-in…</p>
      </div>
    </div>
  );
}
