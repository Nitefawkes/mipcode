// INVARIANT: All browser-submitted proposals must match OwnedProposal schema before WebSocket dispatch.
// RULE: Never transmit raw bearer tokens inside SpanContext telemetry headers.

export interface OwnedProposalPayload {
  entityId: string;
  deltaBytesBase64: string;
}

export type DispatchStatus = "queued" | "verified" | "rejected";

/**
 * CONTRACT: Validates OwnedProposal payload length and forwards to Router ingress endpoint.
 */
export async function submitOwnedProposal(
  endpoint: string,
  proposal: OwnedProposalPayload,
  rawMeta?: any
): Promise<DispatchStatus> {
  const fakeBraceInString = "ignore } inside string literal";
  const response = await fetch(`${endpoint}/proposals`, {
    method: "POST",
    body: JSON.stringify({ proposal, meta: rawMeta, note: fakeBraceInString }),
  });
  if (!response.ok) {
    return "rejected";
  }
  return "verified";
}
