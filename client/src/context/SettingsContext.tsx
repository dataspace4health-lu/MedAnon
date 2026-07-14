/**
 * App-wide settings: the FHIR connection registry + the active connection.
 *
 * This is the single source of truth for which FHIR servers the SPA talks to.
 * It is browser-persisted (per user / per device), see the Settings page.
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
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { FhirConnection } from "@/api/fhirScan";
import { getRuntimeConfig } from "@/api/instanceSettings";
import { setDeploymentConfig } from "@/api/fhirRoute";

export const STORAGE_KEY = "medanon_settings_v1";

/** Prefix marking an active-source id that refers to a backend saved source. */
export const SAVED_SOURCE_PREFIX = "saved:";

const BUILTINS: FhirConnection[] = [
  { id: "source", label: "Source HAPI", kind: "source" },
  { id: "target", label: "Target HAPI (de-identified)", kind: "target" },
];

/** Applied app preferences, every field here actually changes app behavior. */
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
  /** App-wide SOURCE server that browsing + jobs use. A connection id, or
   *  ``saved:<uuid>`` for a backend saved source. Defaults to the built-in. */
  activeSourceId: string;
  /** App-wide TARGET server (built-in or custom connection id). */
  activeTargetId: string;
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
      // A saved:<uuid> source is kept as-is (the backend list is not known here);
      // a plain connection id is only kept when it still exists in the registry.
      const validSel = (id: string | undefined, fallback: string) =>
        id && (id.startsWith(SAVED_SOURCE_PREFIX) || connections.some((c) => c.id === id))
          ? id
          : fallback;
      const activeSourceId = validSel(parsed.activeSourceId, "source");
      const activeTargetId = validSel(parsed.activeTargetId, "target");
      const preferences = { ...DEFAULT_PREFS, ...(parsed.preferences ?? {}) };
      return { connections, activeConnectionId, activeSourceId, activeTargetId, preferences };
    }
  } catch {
    /* fall through to defaults */
  }
  return {
    connections: mergeBuiltins([]),
    activeConnectionId: "source",
    activeSourceId: "source",
    activeTargetId: "target",
    preferences: { ...DEFAULT_PREFS },
  };
}

interface SettingsContextValue {
  connections: FhirConnection[];
  activeConnectionId: string;
  activeConnection: FhirConnection;
  setActiveConnection: (id: string) => void;
  /** App-wide source/target selections driving browsing + jobs. */
  activeSourceId: string;
  activeTargetId: string;
  setActiveSource: (id: string) => void;
  setActiveTarget: (id: string) => void;
  /** Whether the bundled (built-in) source/target FHIR servers exist here. */
  builtinFhirEnabled: boolean;
  /** Whether the active source/target resolves to a usable server. */
  sourceConnected: boolean;
  targetConnected: boolean;
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
  const [builtinFhirEnabled, setBuiltinFhirEnabled] = useState(true);

  const persist = useCallback((next: PersistedShape) => {
    setState(next);
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      /* storage unavailable */
    }
  }, []);

  // Bootstrap deployment-wide config: the routing layer needs the active
  // source/target and whether built-in servers exist. A fresh browser (still on
  // the built-in default) adopts the deployment's active source/target so it uses
  // the client's server, not a bundled HAPI that may not exist.
  useEffect(() => {
    let cancelled = false;
    getRuntimeConfig()
      .then((cfg) => {
        if (cancelled) return;
        setDeploymentConfig(cfg);
        setBuiltinFhirEnabled(cfg.builtin_fhir_enabled);
        setState((prev) => {
          const next = { ...prev };
          if (prev.activeSourceId === "source" && cfg.active_source_id !== "source") {
            next.activeSourceId = cfg.active_source_id;
          }
          if (prev.activeTargetId === "target" && cfg.active_target_id !== "target") {
            next.activeTargetId = cfg.active_target_id;
          }
          if (next.activeSourceId !== prev.activeSourceId || next.activeTargetId !== prev.activeTargetId) {
            try {
              localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
            } catch {
              /* ignore */
            }
            return next;
          }
          return prev;
        });
      })
      .catch(() => {
        /* runtime-config unavailable, keep built-in defaults */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const setActiveConnection = useCallback(
    (id: string) => persist({ ...state, activeConnectionId: id }),
    [state, persist],
  );

  const setActiveSource = useCallback(
    (id: string) => persist({ ...state, activeSourceId: id }),
    [state, persist],
  );

  const setActiveTarget = useCallback(
    (id: string) => persist({ ...state, activeTargetId: id }),
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

  // Whether the active source/target id resolves to a usable server. A bare
  // built-in only counts when the bundled servers exist; 'none'/unset does not.
  const isConnected = (id: string, builtin: "source" | "target"): boolean => {
    if (!id || id === "none") return false;
    if (id === builtin) return builtinFhirEnabled;
    if (id.startsWith(SAVED_SOURCE_PREFIX)) return true;
    return Boolean(state.connections.find((c) => c.id === id)?.baseUrl);
  };
  const sourceConnected = isConnected(state.activeSourceId, "source");
  const targetConnected = isConnected(state.activeTargetId, "target");

  const value = useMemo(
    () => ({
      connections: state.connections,
      activeConnectionId: state.activeConnectionId,
      activeConnection,
      setActiveConnection,
      activeSourceId: state.activeSourceId,
      activeTargetId: state.activeTargetId,
      setActiveSource,
      setActiveTarget,
      builtinFhirEnabled,
      sourceConnected,
      targetConnected,
      upsertConnection,
      removeConnection,
      resetBuiltin,
      preferences: state.preferences,
      setPreference,
    }),
    [state, activeConnection, setActiveConnection, setActiveSource, setActiveTarget, builtinFhirEnabled, sourceConnected, targetConnected, upsertConnection, removeConnection, resetBuiltin, setPreference],
  );

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export function useSettings(): SettingsContextValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be used within <SettingsProvider>");
  return ctx;
}
