import {
  createContext,
  useContext,
  useState,
  useCallback,
  type ReactNode,
} from 'react';
import { STORAGE_KEYS } from '@/config/constants';

interface ConfigContextValue {
  configProfile: string;
  setConfigProfile: (profile: string) => void;
}

const ConfigContext = createContext<ConfigContextValue | null>(null);

export function ConfigProvider({ children }: { children: ReactNode }) {
  const [configProfile, setConfigProfileState] = useState<string>(() => {
    return localStorage.getItem(STORAGE_KEYS.CONFIG_PROFILE) ?? 'auto';
  });

  const setConfigProfile = useCallback((profile: string) => {
    localStorage.setItem(STORAGE_KEYS.CONFIG_PROFILE, profile);
    setConfigProfileState(profile);
  }, []);

  return (
    <ConfigContext.Provider value={{ configProfile, setConfigProfile }}>
      {children}
    </ConfigContext.Provider>
  );
}

export function useConfig(): ConfigContextValue {
  const ctx = useContext(ConfigContext);
  if (!ctx) {
    throw new Error('useConfig must be used within a ConfigProvider');
  }
  return ctx;
}
