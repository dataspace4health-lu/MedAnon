import {
  createContext,
  useContext,
  useState,
  useCallback,
  type ReactNode,
} from 'react';
import { type Role, ROLE_HIERARCHY, STORAGE_KEYS } from '@/config/constants';

interface User {
  name: string;
  roles: Role[];
}

interface AuthContextValue {
  apiKey: string;
  setApiKey: (key: string) => void;
  user: User;
  hasRole: (required: Role) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function buildUser(apiKey: string): User {
  return {
    name: 'local-dev',
    roles: apiKey ? ['admin'] : ['admin'],
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [apiKey, setApiKeyState] = useState<string>(() => {
    return localStorage.getItem(STORAGE_KEYS.API_KEY) ?? '';
  });

  const [user, setUser] = useState<User>(() => buildUser(apiKey));

  const setApiKey = useCallback((key: string) => {
    if (key) {
      localStorage.setItem(STORAGE_KEYS.API_KEY, key);
    } else {
      localStorage.removeItem(STORAGE_KEYS.API_KEY);
    }
    setApiKeyState(key);
    setUser(buildUser(key));
  }, []);

  const hasRole = useCallback(
    (required: Role): boolean => {
      const requiredLevel = ROLE_HIERARCHY.indexOf(required);
      return user.roles.some(
        (role) => ROLE_HIERARCHY.indexOf(role) >= requiredLevel
      );
    },
    [user.roles]
  );

  return (
    <AuthContext.Provider value={{ apiKey, setApiKey, user, hasRole }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return ctx;
}
