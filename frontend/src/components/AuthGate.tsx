import { useEffect, useState, type FormEvent, type PropsWithChildren } from 'react';
import { requestJson } from '../services/api';
import { ErrorState } from './ErrorState';
import { LoadingState } from './LoadingState';

export function AuthGate({ children }: PropsWithChildren) {
  const [state, setState] = useState<'loading' | 'signed-in' | 'sign-in' | 'unavailable'>('loading');
  const [shared, setShared] = useState(false);
  const [retry, setRetry] = useState(0);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    const unauthorized = () => setState('sign-in');
    window.addEventListener('ctf:unauthorized', unauthorized);
    const check = async () => {
      try {
        const { required } = await requestJson<{ required: boolean }>('/auth/session', { signal: controller.signal });
        if (controller.signal.aborted) return;
        setShared(required);
        if (required) await requestJson('/projects?limit=1', { signal: controller.signal });
        if (!controller.signal.aborted) setState('signed-in');
      } catch (err) {
        if (controller.signal.aborted) return;
        if ((err as { statusCode?: number }).statusCode !== 401) {
          setError((err as Error).message);
          setState('unavailable');
        }
      }
    };
    void check();
    return () => {
      controller.abort();
      window.removeEventListener('ctf:unauthorized', unauthorized);
    };
  }, [retry]);

  const signIn = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const token = String(new FormData(form).get('access') || '').trim();
    setPending(true);
    setError('');
    try {
      await requestJson('/auth/session', { method: 'POST', headers: { Authorization: 'Bearer ' + token } });
      form.reset();
      // Check that the browser accepted the Secure cookie before showing private views.
      await requestJson('/projects?limit=1');
      setState('signed-in');
    } catch (err) { setError((err as Error).message); }
    finally { setPending(false); }
  };
  const signOut = async () => {
    setError('');
    try {
      await requestJson('/auth/session', { method: 'DELETE' });
      try { localStorage.removeItem('ctf.selection.v1'); } catch { /* Private browsing. */ }
      setState('sign-in');
    } catch (err) { setError((err as Error).message); }
  };

  if (state === 'signed-in') return <>
    {children}
    {shared && <div className="session-actions">
      {error && <p role="alert">{error}</p>}
      <button className="btn btn-secondary" onClick={() => void signOut()}>Sign out</button>
    </div>}
  </>;
  return <main className="auth-panel">
    {state === 'loading' && <LoadingState message="Connecting to MIRAI…" />}
    {state === 'unavailable' && <ErrorState message={error} onRetry={() => { setState('loading'); setRetry(n => n + 1); }} />}
    {state === 'sign-in' && <form className="card workflow-panel" onSubmit={signIn}>
      <h1>Sign in to MIRAI</h1>
      <label htmlFor="workflow-access">Access token</label>
      <input id="workflow-access" name="access" type="password" autoComplete="current-password" required maxLength={128} />
      {error && <p role="alert">{error}</p>}
      <button className="btn btn-primary" disabled={pending}>{pending ? 'Signing in…' : 'Sign in'}</button>
    </form>}
  </main>;
}
