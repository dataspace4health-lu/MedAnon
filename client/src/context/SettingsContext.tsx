/**
 * App-wide settings: the FHIR connection registry + the active connection.
 *
 * This is the single source of truth for which FHIR servers the SPA talks to.
 * It is browser-persisted (per user / per device) — see the Settings page.
 *
 * Built-in connections:
 *   - `source` → the source HAPI server (nginx /fhir proxy)
 *   - `target` → the de-identified target server (nginx /fhir-target proxy)
 * Both are always present and cannot be deleted, but the user may attach an
 * override base URL + token to route them through the backend FHIR proxy
 * instead (i.e. point "Source" at a different server). Custom connections are
 * fully user-defined and reach external servers through the SSRF-guarded proxy.
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { FhirConnection } from "@/api/fhirScan";

const STORAGE_KEY = "medanon_settings_v1";

const BUILTINS: FhirConnection[] = [
  { id: "source", label: "Source HAPI", kind: "source" },
  { id: "target", label: "Target HAPI (de-identified)", kind: "target" },
];

/** Applied app preferences — every field here actually changes app behavior. */
export interface AppPreferences {
  /** Pre-fills the Trust Gate dataset id. */
  datasetId: string;
  /** Pre-fills the Trust Gate provenance "source system". */
  sourceSystem: string;
  /** Default cap for a full-server scan. */
  scanMaxResources: number;
  /** Default page size for FHIR fetches (browsers + scan paging). */
  fhirPageSize: number;
}

const DEFAULT_PREFS: AppPreferences = {
  datasetId: "fhir-dataset",
  sourceSystem: "hapi-fhir (source)",
  scanMaxResources: 20000,
  fhirPageSize: 1000,
};

interface PersistedShape {
  connections: FhirConnection[];
  activeConnectionId: string;
  preferences: AppPreferences;
}

function mergeBuiltins(stored: FhirConnection[]): FhirConnection[] {
  // Keep built-ins first (with any persisted overrides), then customs.
  const byId = new Map(stored.map((c) => [c.id, c]));
  const builtins = BUILTINS.map((b) => ({ ...b, ...byId.get(b.id), kind: b.kind, id: b.id }));
  const customs = stored.filter((c) => c.kind === "custom" && c.baseUrl);
  return [...builtins, ...customs];
}

function load(): PersistedShape {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as PersistedShape;
      const connections = mergeBuiltins(parsed.connections ?? []);
      const activeConnectionId = connections.some((c) => c.id === parsed.activeConnectionId)
        ? parsed.activeConnectionId
        : "source";
      const preferences = { ...DEFAULT_PREFS, ...(parsed.preferences ?? {}) };
      return { connections, activeConnectionId, preferences };
    }
  } catch {
    /* fall through to defaults */
  }
  return { connections: mergeBuiltins([]), activeConnectionId: "source", preferences: { ...DEFAULT_PREFS } };
}

interface SettingsContextValue {
  connections: FhirConnection[];
  activeConnectionId: string;
  activeConnection: FhirConnection;
  setActiveConnection: (id: string) => void;
  /** Add or update a connection (matched by id). Returns the saved connection. */
  upsertConnection: (conn: FhirConnection) => void;
  removeConnection: (id: string) => void;
  /** Clear an override on a built-in (revert Source/Target to the nginx proxy). */
  resetBuiltin: (id: string) => void;
  preferences: AppPreferences;
  setPreference: <K extends keyof AppPreferences>(key: K, value: AppPreferences[K]) => void;
}

const SettingsContext = createContext<SettingsContextValue | null>(null);

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<PersistedShape>(() => load());

  const persist = useCallback((next: PersistedShape) => {
    setState(next);
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      /* storage unavailable */
    }
  }, []);

  const setActiveConnection = useCallback(
    (id: string) => persist({ ...state, activeConnectionId: id }),
    [state, persist],
  );

  const upsertConnection = useCallback(
    (conn: FhirConnection) => {
      const exists = state.connections.some((c) => c.id === conn.id);
      const connections = exists
        ? state.connections.map((c) => (c.id === conn.id ? { ...c, ...conn } : c))
        : [...state.connections, conn];
      persist({ ...state, connections });
    },
    [state, persist],
  );

  const removeConnection = useCallback(
    (id: string) => {
      if (id === "source" || id === "target") return; // built-ins are permanent
      const connections = state.connections.filter((c) => c.id !== id);
      const activeConnectionId = state.activeConnectionId === id ? "source" : state.activeConnectionId;
      persist({ ...state, connections, activeConnectionId });
    },
    [state, persist],
  );

  const resetBuiltin = useCallback(
    (id: string) => {
      const connections = state.connections.map((c) =>
        c.id === id ? { id: c.id, label: c.label, kind: c.kind } : c,
      );
      persist({ ...state, connections });
    },
    [state, persist],
  );

  const setPreference = useCallback<SettingsContextValue["setPreference"]>(
    (key, value) => persist({ ...state, preferences: { ...state.preferences, [key]: value } }),
    [state, persist],
  );

  const activeConnection =
    state.connections.find((c) => c.id === state.activeConnectionId) ?? state.connections[0];

  const value = useMemo(
    () => ({
      connections: state.connections,
      activeConnectionId: state.activeConnectionId,
      activeConnection,
      setActiveConnection,
      upsertConnection,
      removeConnection,
      resetBuiltin,
      preferences: state.preferences,
      setPreference,
    }),
    [state, activeConnection, setActiveConnection, upsertConnection, removeConnection, resetBuiltin, setPreference],
  );

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export function useSettings(): SettingsContextValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be used within <SettingsProvider>");
  return ctx;
}
