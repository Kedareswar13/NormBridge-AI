import React, { useEffect, useState } from "react";

const KEY = "pp-theme";

export function applyStoredTheme() {
  const theme = localStorage.getItem(KEY) || "dark";
  document.documentElement.classList.toggle("pp-dark", theme === "dark");
  return theme;
}

/** Sun/moon button that switches between the dark (default) and light themes. */
export default function ThemeToggle({ className = "" }) {
  const [theme, setTheme] = useState(() => localStorage.getItem(KEY) || "dark");

  useEffect(() => {
    document.documentElement.classList.toggle("pp-dark", theme === "dark");
    localStorage.setItem(KEY, theme);
  }, [theme]);

  const dark = theme === "dark";
  return (
    <button
      type="button"
      className={`pp-theme-toggle ${className}`}
      onClick={() => setTheme(dark ? "light" : "dark")}
      title={dark ? "Switch to light theme" : "Switch to dark theme"}
      aria-label="Toggle colour theme"
    >
      {dark ? (
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
        </svg>
      ) : (
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  );
}
