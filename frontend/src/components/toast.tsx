import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

import { cx } from "./ui";

type Toast = { id: number; tone: "good" | "bad"; text: string };

const ToastContext = createContext<(tone: Toast["tone"], text: string) => void>(() => {});

/** Brief confirmation after an action ("3 invoices settled"), or its error. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((tone: Toast["tone"], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, tone, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div aria-live="polite" className="fixed right-4 bottom-4 z-50 flex max-w-sm flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={cx(
              "rounded-lg border border-line px-3 py-2 text-sm shadow-lg",
              t.tone === "good" ? "bg-surface text-ink" : "bg-bad-soft text-bad",
            )}
          >
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);
