// @vitest-environment node
import { describe, expect, it } from "vitest";

import {
  ProxyFailure,
  proxyFailureMessage,
  semanticFallbackNotice,
} from "./proxy-errors";

describe("proxyFailureMessage", () => {
  it("explains an unreachable upstream", () => {
    expect(proxyFailureMessage("upstream_unreachable", 502, "Monitor")).toEqual(
      "Monitor is unavailable — the monitor service or its network tunnel may be offline. Try again shortly.",
    );
  });

  it("reports an upstream timeout", () => {
    expect(proxyFailureMessage("upstream_timeout", 504, "Monitor")).toEqual(
      "Monitor timed out. The monitor service may be under load — try again shortly.",
    );
  });

  it("reports a missing configuration", () => {
    expect(proxyFailureMessage("not_configured", 500, "Monitor")).toEqual(
      "Monitor is not configured on this deployment. Contact the operator.",
    );
  });

  it("maps an unauthorized code to the reauth copy", () => {
    expect(proxyFailureMessage("unauthorized", 401, "Monitor")).toEqual(
      "Your session is no longer authorized. Sign in again.",
    );
  });

  it("infers unauthorized from status 401 when code is null", () => {
    expect(proxyFailureMessage(null, 401, "Monitor")).toEqual(
      "Your session is no longer authorized. Sign in again.",
    );
  });

  it("infers forbidden from status 403 when code is null", () => {
    expect(proxyFailureMessage(null, 403, "Monitor")).toEqual(
      "Your session doesn't have operator access.",
    );
  });

  it("infers not_configured from status 503 when code is null", () => {
    expect(proxyFailureMessage(null, 503, "Monitor")).toEqual(
      "Monitor is not configured on this deployment. Contact the operator.",
    );
  });

  it("maps upstream_error to the generic error copy", () => {
    expect(proxyFailureMessage("upstream_error", 502, "Monitor")).toEqual(
      "Monitor returned an error. Try again shortly.",
    );
  });

  it("maps upstream_invalid_response to the generic error copy", () => {
    expect(
      proxyFailureMessage("upstream_invalid_response", 502, "Monitor"),
    ).toEqual("Monitor returned an error. Try again shortly.");
  });

  it("falls back for an unknown code", () => {
    expect(proxyFailureMessage("something_else", 500, "Monitor")).toEqual(
      "Monitor is unavailable. Try again shortly.",
    );
  });
});

describe("semanticFallbackNotice", () => {
  it("appends the keyword-results notice to the semantic search copy", () => {
    expect(semanticFallbackNotice("upstream_timeout", 504)).toEqual(
      "Semantic search timed out. The monitor service may be under load — try again shortly. Showing keyword results.",
    );
  });
});

describe("ProxyFailure", () => {
  it("exposes code, status, and the class name", () => {
    const failure = new ProxyFailure("upstream_timeout", 504);
    expect(failure.name).toEqual("ProxyFailure");
    expect(failure.code).toEqual("upstream_timeout");
    expect(failure.status).toEqual(504);
  });
});
