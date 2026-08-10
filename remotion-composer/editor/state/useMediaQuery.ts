import { useEffect, useState } from "react";

/**
 * Live-subscribed media query — unlike App.tsx's own detectDesktopPointer()
 * (deliberately read ONCE via useMemo, because pointer *capability*
 * shouldn't change mid-session in any way that matters), this one exists
 * specifically because *width* genuinely does change live: rotating a
 * phone, resizing a desktop window across the phone/desktop shell
 * boundary, or DevTools device-mode toggling all need the shell to react
 * immediately, not just on next reload.
 *
 * Initial value is computed SYNCHRONOUSLY (in useState's lazy initializer,
 * not an effect) so there is never a first-paint flash of the wrong shell —
 * this is a pure client SPA (main.tsx calls createRoot().render() directly,
 * no SSR), so window.matchMedia is always available at first render.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange(); // query string itself may have changed since the lazy initializer ran
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}
