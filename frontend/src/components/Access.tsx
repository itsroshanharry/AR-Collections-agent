import { useEffect, useRef, useState } from "react";

import { roleLabel, useAuth, type Role } from "../lib/auth";
import { useToast } from "./toast";
import { Button, ErrorNote, inputClass } from "./ui";

/** Sidebar block: who you are, sign in or out, and switch to a lower role for a demo. */
export function AccessPanel() {
  const { me, role, signOut, actAs } = useAuth();
  const [open, setOpen] = useState(false);

  if (!me) return null;
  if (!me.auth_enabled) {
    return (
      <p className="text-xs text-muted" title="No API keys are configured on the server">
        Open access · {roleLabel(role)}
      </p>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs md:flex-col md:items-start">
      {me.authenticated ? (
        <>
          <label className="flex items-center gap-1.5">
            <span className="text-muted">Acting as</span>
            <select
              className="rounded-md border border-line bg-surface px-1.5 py-0.5 text-xs"
              value={role}
              onChange={(e) => actAs(e.target.value as Role)}
              aria-label="Act as role"
            >
              {me.can_act_as.map((r) => (
                <option key={r} value={r}>
                  {roleLabel(r as Role)}
                </option>
              ))}
            </select>
          </label>
          <button className="text-accent hover:underline" onClick={signOut}>
            Sign out
          </button>
        </>
      ) : (
        <>
          <span className="text-muted">{roleLabel(role)} · read-only</span>
          <button className="text-accent hover:underline" onClick={() => setOpen(true)}>
            Sign in
          </button>
        </>
      )}
      {open && <SignInDialog onClose={() => setOpen(false)} />}
    </div>
  );
}

function SignInDialog({ onClose }: { onClose: () => void }) {
  const { signIn } = useAuth();
  const toast = useToast();
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  return (
    <dialog
      ref={dialog}
      onClose={onClose}
      className="m-auto w-[min(26rem,calc(100vw-2rem))] rounded-xl border border-line bg-surface p-0 text-ink shadow-xl backdrop:bg-black/40"
    >
      <form
        className="flex flex-col gap-3 p-5"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            const me = await signIn(key.trim());
            toast("good", `Signed in as ${roleLabel(me.role as Role).toLowerCase()}.`);
            onClose();
          } catch (err) {
            setError(err);
          } finally {
            setBusy(false);
          }
        }}
      >
        <h2 className="text-base font-semibold">Sign in</h2>
        <p className="text-sm text-muted">
          Anyone can look around. To record outcomes, post payments or work the queues, paste an operator or admin
          key. It is kept in this browser only.
        </p>
        <input
          autoFocus
          type="password"
          autoComplete="current-password"
          className={inputClass}
          placeholder="API key"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          aria-label="API key"
        />
        {error ? <ErrorNote error={error} /> : null}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={() => dialog.current?.close()}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy} disabled={!key.trim()}>
            Sign in
          </Button>
        </div>
      </form>
    </dialog>
  );
}

/** A strip above every page for visitors who cannot act, so disabled buttons are not a
 *  mystery. */
export function ReadOnlyNotice() {
  const { me, can } = useAuth();
  if (!me || can("operator")) return null;
  return (
    <div className="mb-5 rounded-lg border border-line bg-info-soft px-3 py-2 text-sm text-info">
      {me.authenticated
        ? "You are acting as a viewer: everything is visible, and actions are switched off."
        : "This is a read-only demo: everything is visible, and actions need a sign-in."}
    </div>
  );
}
