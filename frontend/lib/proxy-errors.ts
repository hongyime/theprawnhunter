export class ProxyFailure extends Error {
  readonly code: string | null;
  readonly status: number;

  constructor(code: string | null, status: number) {
    super(code ?? `http_${status}`);
    this.name = "ProxyFailure";
    this.code = code;
    this.status = status;
  }
}

/**
 * Human copy for a same-origin proxy failure. The monitor API sits behind a
 * home tunnel, so an unreachable upstream is usually that link, not the browser.
 */
export function proxyFailureMessage(
  code: string | null | undefined,
  status: number,
  subject: string,
): string {
  const effective =
    code ??
    (status === 401
      ? "unauthorized"
      : status === 403
        ? "forbidden"
        : status === 503
          ? "not_configured"
          : null);

  switch (effective) {
    case "upstream_unreachable":
      return `${subject} is unavailable — the monitor service or its network tunnel may be offline. Try again shortly.`;
    case "upstream_timeout":
      return `${subject} timed out. The monitor service may be under load — try again shortly.`;
    case "not_configured":
      return `${subject} is not configured on this deployment. Contact the operator.`;
    case "unauthorized":
      return "Your session is no longer authorized. Sign in again.";
    case "forbidden":
      return "Your session doesn't have operator access.";
    case "upstream_error":
    case "upstream_invalid_response":
      return `${subject} returned an error. Try again shortly.`;
    default:
      return `${subject} is unavailable. Try again shortly.`;
  }
}

export function semanticFallbackNotice(
  code: string | null | undefined,
  status: number,
): string {
  return `${proxyFailureMessage(code, status, "Semantic search")} Showing keyword results.`;
}
