import { createContext, useContext, useMemo, useCallback, type ReactNode } from "react";
import { ThemeProvider as MuiThemeProvider } from "@mui/material/styles";
import CssBaseline from "@mui/material/CssBaseline";
import {
  ThemeProvider as KuiThemeProvider,
  useTheme as useKuiTheme,
} from "@nvidia/foundations-react-core";
import { getTheme, type ThemeMode } from "../theme";

interface ThemeContextValue {
  mode: ThemeMode;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  mode: "dark",
  toggleTheme: () => {},
});

export const useThemeMode = () => useContext(ThemeContext);

const getInitialMode = (): ThemeMode => {
  try {
    const stored = localStorage.getItem("nvis-rack-theme");
    if (stored === "light" || stored === "dark") return stored;
  } catch {}
  return "dark";
};

const ThemeBridge = ({ children }: { children: ReactNode }) => {
  const { theme, setTheme } = useKuiTheme();

  const mode = (theme === "light" ? "light" : "dark") as ThemeMode;

  const toggleTheme = useCallback(() => {
    const next: ThemeMode = mode === "dark" ? "light" : "dark";
    setTheme(next);
    localStorage.setItem("nvis-rack-theme", next);
  }, [mode, setTheme]);

  const muiTheme = useMemo(() => getTheme(mode), [mode]);
  const contextValue = useMemo(() => ({ mode, toggleTheme }), [mode, toggleTheme]);

  return (
    <ThemeContext.Provider value={contextValue}>
      <MuiThemeProvider theme={muiTheme}>
        <CssBaseline />
        {children}
      </MuiThemeProvider>
    </ThemeContext.Provider>
  );
};

interface ThemeProviderProps {
  children: ReactNode;
}

export const ThemeProvider = ({ children }: ThemeProviderProps) => (
  <KuiThemeProvider theme={getInitialMode()} global>
    <ThemeBridge>{children}</ThemeBridge>
  </KuiThemeProvider>
);
