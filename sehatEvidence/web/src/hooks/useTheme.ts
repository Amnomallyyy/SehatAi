import { useCallback, useEffect, useState } from "react";

type Theme = "dark" | "light";
const STORAGE_KEY = "evidenceboard-theme";

function applyTheme(theme: Theme) {
  if (theme === "light") {
    document.documentElement.setAttribute("data-theme", "light");
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}

function readStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // localStorage can throw in a private-browsing / storage-blocked
    // context -- fall through to the dark default below.
  }
  return "dark";
}

/** Dark is the validated default; this is a per-viewer preference only,
 * stored client-side (never sent anywhere) -- unrelated to the
 * server-side history/cache persistence elsewhere in the app. */
export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(() => readStoredTheme());

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // best-effort persistence only
    }
  }, []);

  const toggle = useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [theme, setTheme]);

  return { theme, setTheme, toggle };
}
