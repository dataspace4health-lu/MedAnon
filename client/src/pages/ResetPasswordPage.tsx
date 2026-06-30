/**
 * Password reset is now handled entirely by Keycloak's hosted login UI.
 * Users reach the "Forgot password?" link from Keycloak's login page,
 * which is shown when they click "Sign in" on LoginPage.
 *
 * This stub exists only to handle old bookmarks gracefully.
 */

import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';

export default function ResetPasswordPage() {
  const navigate = useNavigate();
  useEffect(() => {
    navigate('/login', { replace: true });
  }, [navigate]);
  return null;
}
