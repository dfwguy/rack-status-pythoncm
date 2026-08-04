/**
 * Auth provider: checks the session once on mount, then exposes login/logout.
 *
 * Components only; the context object and `useAuth` live in AuthContext.ts.
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { AuthContext, type AuthState } from "./AuthContext";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    authenticated: false,
    user: null,
    loading: true,
  });

  useEffect(() => {
    fetch("/api/auth-check")
      .then((res) => {
        if (res.ok) return res.json();
        throw new Error("not authenticated");
      })
      .then((data) => {
        setState({ authenticated: true, user: data.user, loading: false });
      })
      .catch(() => {
        setState({ authenticated: false, user: null, loading: false });
      });
  }, []);

  const login = useCallback(async (password: string) => {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const data = await res.json();
    if (data.ok) {
      setState({ authenticated: true, user: data.user, loading: false });
      return { ok: true };
    }
    return { ok: false, error: data.error || "Login failed" };
  }, []);

  const logout = useCallback(async () => {
    await fetch("/api/logout", { method: "POST" });
    setState({ authenticated: false, user: null, loading: false });
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}
