/** Explicit captured identity for asynchronous requests; never re-read localStorage. */
export interface CapturedTransport { token: string; signal: AbortSignal }
export const transportConfig = (context?: CapturedTransport) => context ? ({
  headers: { Authorization: `Bearer ${context.token}` }, signal: context.signal,
}) : {};
