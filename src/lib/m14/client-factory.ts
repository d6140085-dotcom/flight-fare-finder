// Chooses the subscriptions client. Phase 3A has only the mock; there is deliberately no
// fallback: outside preview builds this throws instead of quietly returning mock data, and the
// HTTP client arrives in Phase 3B (docs/m1.4-phase3a-frontend-mock.md §5).
import { MockSubscriptionsClient, type MockOptions } from "./mock-client";
import { mockDataAllowed } from "./routes";

export class ClientUnavailable extends Error {
  constructor() {
    super("No subscriptions API client is configured for this build (Phase 3B).");
    this.name = "ClientUnavailable";
  }
}

export function createSubscriptionsClient(
  opts: MockOptions & { allowMock?: boolean } = {},
): MockSubscriptionsClient {
  const allowMock = opts.allowMock ?? mockDataAllowed();
  if (!allowMock) throw new ClientUnavailable();
  return new MockSubscriptionsClient(opts);
}
