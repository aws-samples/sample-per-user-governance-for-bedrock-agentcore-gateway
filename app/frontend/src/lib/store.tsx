// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  ReactNode,
} from "react";
import { api } from "./api";
import { clearToken, getToken, signIn as cognitoSignIn } from "./auth";
import {
  AppConfig,
  Capabilities,
  DEFAULT_WARN_PCT,
  LadderState,
  PersonaState,
  ladderState,
} from "./types";

export type TabId = "demo" | "fleet";

interface Store {
  tab: TabId;
  setTab: (t: TabId) => void;
  unlocked: boolean;
  signIn: (username: string, password: string) => Promise<string | null>;
  signOut: () => void;
  personas: PersonaState[];
  activePersona: PersonaState | null;
  activeState: LadderState;
  switchPersona: (id: string) => void;
  refresh: () => Promise<void>;
  applyPersonaState: (state: PersonaState) => void;
  capabilities: Capabilities;
  config: AppConfig | null;
  warnPct: (personaId: string) => number;
  setWarnPct: (personaId: string, pct: number) => void;
  loadError: string | null;
}

const Ctx = createContext<Store | null>(null);

// The Claude Code pane goes browser -> runtime directly, so its capability
// comes from the frontend build env, not from the backend.
const CLAUDECODE_ENABLED = Boolean(import.meta.env.VITE_CLAUDECODE_RUNTIME_ARN);

export function StoreProvider({ children }: { children: ReactNode }) {
  const [tab, setTab] = useState<TabId>("demo");
  const [unlocked, setUnlocked] = useState(false);
  const [personas, setPersonas] = useState<PersonaState[]>([]);
  const [activeId, setActiveId] = useState<string>("engineer");
  const [capabilities, setCapabilities] = useState<Capabilities>({
    strands: true,
    langgraph: true,
    claudecode: CLAUDECODE_ENABLED,
  });
  const [config, setConfig] = useState<AppConfig | null>(null);
  // The warn band is a display threshold only; the gateway enforces downgrade
  // and pause. It is therefore client-side state.
  const [warnPcts, setWarnPcts] = useState<Record<string, number>>({});
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.getPersonas();
      setPersonas(data.personas);
      if (data.personas[0]) setActiveId(data.personas[0].persona.id);
      setCapabilities({ ...data.capabilities, claudecode: CLAUDECODE_ENABLED });
      setConfig(data.config);
      setLoadError(null);
      setUnlocked(true);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "";
      if (msg === "signed-out") {
        clearToken();
        setUnlocked(false);
        return;
      }
      setLoadError(
        "Could not load personas. Check that the infra stack is deployed, the API is reachable, and the Lambda has Cognito and DynamoDB access. " +
          (msg ? `(${msg})` : "")
      );
    }
  }, []);

  const signIn = useCallback(
    async (username: string, password: string): Promise<string | null> => {
      const result = await cognitoSignIn(username, password);
      if (!result.ok) return result.message ?? "Sign-in failed.";
      try {
        const data = await api.getPersonas();
        setPersonas(data.personas);
        if (data.personas[0]) setActiveId(data.personas[0].persona.id);
        setCapabilities({ ...data.capabilities, claudecode: CLAUDECODE_ENABLED });
        setConfig(data.config);
        setLoadError(null);
        setUnlocked(true);
        return null;
      } catch (e) {
        clearToken();
        return e instanceof Error && e.message !== "signed-out"
          ? `Signed in, but the API failed: ${e.message}`
          : "Signed in, but the API rejected the token.";
      }
    },
    []
  );

  const signOut = useCallback(() => {
    clearToken();
    setUnlocked(false);
  }, []);

  useEffect(() => {
    if (getToken()) void refresh();
  }, [refresh]);

  const applyPersonaState = useCallback((state: PersonaState) => {
    setPersonas((list) => {
      const idx = list.findIndex((p) => p.persona.id === state.persona.id);
      if (idx < 0) return [...list, state];
      const next = [...list];
      next[idx] = state;
      return next;
    });
  }, []);

  const warnPct = useCallback(
    (personaId: string) => warnPcts[personaId] ?? DEFAULT_WARN_PCT,
    [warnPcts]
  );
  const setWarnPct = useCallback((personaId: string, pct: number) => {
    setWarnPcts((m) => ({ ...m, [personaId]: pct }));
  }, []);

  const activePersona = personas.find((p) => p.persona.id === activeId) ?? null;
  const activeState: LadderState = activePersona
    ? ladderState(activePersona, warnPct(activeId))
    : "allowed";

  const value = useMemo<Store>(
    () => ({
      tab,
      setTab,
      unlocked,
      signIn,
      signOut,
      personas,
      activePersona,
      activeState,
      switchPersona: setActiveId,
      refresh,
      applyPersonaState,
      capabilities,
      config,
      warnPct,
      setWarnPct,
      loadError,
    }),
    [
      tab,
      unlocked,
      signIn,
      signOut,
      personas,
      activePersona,
      activeState,
      refresh,
      applyPersonaState,
      capabilities,
      config,
      warnPct,
      setWarnPct,
      loadError,
    ]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore(): Store {
  const s = useContext(Ctx);
  if (!s) throw new Error("useStore outside StoreProvider");
  return s;
}
