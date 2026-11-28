import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, api, setCredentials, unwrap, type Schemas } from "../api/client";

export type Role = "viewer" | "operator" | "admin";
const RANK: Record<Role, number> = { viewer: 0, operator: 1, admin: 2 };

const KEY_STORE = "ar-console.api-key";
const ACT_AS_STORE = "ar-console.act-as";

/** Browser storage can be unavailable (private windows, blocked site data): the console
 *  then still works, it just forgets the key on reload. */
function load(name: string): string | null {
  try {
    return window.localStorage.getItem(name);
  } catch {
    return null;
  }
}

function save(name: string, value: string | null): void {
  try {
    if (value) window.localStorage.setItem(name, value);
    else window.localStorage.removeItem(name);
  } catch {
    /* not persisted; fine */
  }
}

type Auth = {
  me: Schemas["MeOut"] | undefined;
  role: Role;
  /** Is the caller allowed to do something that needs `need`? */
  can: (need: Role) => boolean;
  signIn: (key: string) => Promise<Schemas["MeOut"]>;
  signOut: () => void;
  actAs: (role: Role) => void;
};

const AuthContext = createContext<Auth | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [key, setKey] = useState<string | null>(() => load(KEY_STORE));
  const [actAsRole, setActAsRole] = useState<Role | null>(() => load(ACT_AS_STORE) as Role | null);

  // Every request the typed client makes carries the current credentials.
  setCredentials(key, actAsRole);

  const { data: me, error } = useQuery({
    queryKey: ["me", key, actAsRole],
    queryFn: async () => unwrap(await api.GET("/auth/me")),
    staleTime: 60_000,
    retry: false,
    // Keep showing the last answer while a new key is checked, so the sidebar and every
    // button do not flicker to "signed out" in between.
    placeholderData: keepPreviousData,
  });

  const signOut = useCallback(() => {
    save(KEY_STORE, null);
    save(ACT_AS_STORE, null);
    setKey(null);
    setActAsRole(null);
  }, []);

  // A stored key that stops working (rotated on the server) signs the user out rather
  // than leaving every request failing.
  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) signOut();
  }, [error, signOut]);

  // Whatever a role change unlocks or hides, every screen should refetch. Not on the
  // first render: nothing has been fetched with other credentials yet.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    void queryClient.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "me" });
  }, [key, actAsRole, queryClient]);

  const signIn = useCallback(async (candidate: string) => {
    // Check the key before keeping it: /auth/me answers 401 for a wrong one.
    const res = await fetch("/auth/me", { headers: { "X-API-Key": candidate } });
    if (!res.ok) throw new ApiError(res.status, res.status === 401 ? "That key is not valid." : "Could not check the key.");
    const body = (await res.json()) as Schemas["MeOut"];
    save(KEY_STORE, candidate);
    save(ACT_AS_STORE, null);
    setKey(candidate);
    setActAsRole(null);
    return body;
  }, []);

  const actAs = useCallback(
    (role: Role) => {
      // Acting as your own role is the same as no override.
      const top = me?.can_act_as[me.can_act_as.length - 1];
      const value = role === top ? null : role;
      save(ACT_AS_STORE, value);
      setActAsRole(value);
    },
    [me],
  );

  const value = useMemo<Auth>(() => {
    const role = (me?.role ?? "viewer") as Role;
    return { me, role, can: (need) => !!me && RANK[role] >= RANK[need], signIn, signOut, actAs };
  }, [me, signIn, signOut, actAs]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth outside AuthProvider");
  return auth;
}

export function roleLabel(role: Role): string {
  return { viewer: "Viewer", operator: "Operator", admin: "Admin" }[role];
}
