/**
 * Login page, Authorization Code Flow + PKCE.
 *
 * In OIDC mode: clicking "Sign in" calls UserManager.signinRedirect() which
 * sends the user to Keycloak's login page.  Keycloak handles credentials,
 * MFA, and password reset; our SPA never sees raw passwords.
 *
 * In apikey mode: show an API key entry form.
 * In auto/none mode: redirect immediately (no login needed).
 */

import { useState, useEffect, type FormEvent } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { ShieldCheck, Loader2, Key, AlertCircle, LogIn } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

interface LocationState {
  from?: { pathname: string };
}

// ── Shared brand header ───────────────────────────────────────────────────

function Brand() {
  return (
    <div className="mb-8 flex flex-col items-center text-center">
      <span className="flex size-12 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-sm">
        <ShieldCheck className="size-6" />
      </span>
      <h1 className="mt-4 text-xl font-semibold tracking-tight text-foreground">
        Data&nbsp;Privacy&nbsp;Toolkit
      </h1>
      <p className="mt-1 text-sm text-muted-foreground">Sign in to continue</p>
    </div>
  );
}

// ── OIDC panel, single "Sign in" button ─────────────────────────────────

function OidcPanel({ onLogin }: { onLogin: () => void }) {
  const [busy, setBusy] = useState(false);

  function handleClick() {
    setBusy(true);
    // signinRedirect() navigates away, no need to reset busy.
    onLogin();
  }

  return (
    <div className="rounded-2xl border border-border bg-card p-6 shadow-sm">
      <Button
        size="lg"
        className="h-11 w-full gap-2"
        onClick={handleClick}
        disabled={busy}
      >
        {busy ? (
          <>
            <Loader2 className="size-4 animate-spin" />
            Redirecting…
          </>
        ) : (
          <>
            <LogIn className="size-4" />
            Sign in with your organisation
          </>
        )}
      </Button>
      <p className="mt-4 text-center text-xs text-muted-foreground">
        You will be redirected to your identity provider to authenticate.
      </p>
    </div>
  );
}

// ── API-key panel ─────────────────────────────────────────────────────────

function ApiKeyPanel({ onSave }: { onSave: (key: string) => void }) {
  const [key, setKey] = useState('');
  const [error, setError] = useState<string | null>(null);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!key.trim()) {
      setError('Enter your API key.');
      return;
    }
    onSave(key.trim());
  }

  return (
    <div className="rounded-2xl border border-border bg-card p-6 shadow-sm">
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        <div className="space-y-1.5">
          <label htmlFor="apikey" className="text-sm font-medium text-foreground">
            API Key
          </label>
          <div className="relative">
            <Key className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="apikey"
              type="password"
              autoComplete="current-password"
              autoFocus
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="sk-…"
              className="h-10 pl-9"
            />
          </div>
        </div>

        {error && (
          <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            <AlertCircle className="size-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <Button type="submit" size="lg" className="h-10 w-full">
          Continue
        </Button>
      </form>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────

export default function LoginPage() {
  const { mode, loading, isAuthenticated, login, setApiKey } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const from = (location.state as LocationState | null)?.from?.pathname ?? '/';

  // Already authenticated or no auth required, bounce home.
  useEffect(() => {
    if (!loading && isAuthenticated) {
      navigate(from, { replace: true });
    }
    if (!loading && (mode === 'auto' || mode === 'none')) {
      navigate('/', { replace: true });
    }
  }, [loading, isAuthenticated, mode, navigate, from]);

  if (loading || isAuthenticated || mode === 'auto' || mode === 'none') {
    return (
      <div className="fixed inset-0 flex items-center justify-center bg-background">
        <Loader2 className="size-8 animate-spin text-primary" />
      </div>
    );
  }

  function handleApiKeySave(key: string) {
    setApiKey(key);
    navigate(from, { replace: true });
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-background via-background to-muted/40 px-4">
      <div className="w-full max-w-sm">
        <Brand />

        {mode === 'oidc' ? (
          <OidcPanel onLogin={() => login(from)} />
        ) : (
          <ApiKeyPanel onSave={handleApiKeySave} />
        )}

        <p className="mt-6 text-center text-xs text-muted-foreground">
          Protected by Keycloak · Data&nbsp;Privacy&nbsp;Toolkit
        </p>
      </div>
    </div>
  );
}
