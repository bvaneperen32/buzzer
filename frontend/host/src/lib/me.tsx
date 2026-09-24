import { createContext, useContext } from 'react';

/** The signed-in user, from `GET /auth/me`. For display only — the backend enforces every rule. */
export interface Me {
  id: string;
  username: string | null;
  netid: string | null;
  display_name: string | null;
  role: string;
}

/**
 * Provided by `HostShell` once `/auth/me` has loaded. Held in memory only: never write the
 * role to localStorage, where it would go stale the same way the token's role claim does.
 */
export const MeContext = createContext<Me | null>(null);

export function useMe(): Me {
  const me = useContext(MeContext);
  if (!me) throw new Error('useMe() must be used inside HostShell');
  return me;
}
