---
name: PropLine endpoint safety
description: How the authorized PropLine PrizePicks adapter handles endpoint configuration.
---

The PropLine API endpoint must be supplied explicitly through `PROPLINE_API_URL`. A key alone is not enough to justify requests to a guessed host; without an endpoint the provider stays disabled.

**Why:** Public searches did not establish a PrizePicks PropLine API contract, and the obvious propline.com host is not a usable API endpoint. Sending the credential to an invented or unrelated host would be unsafe.

**How to apply:** Keep the adapter configurable and fail closed when the endpoint is absent. Only report live PrizePicks observations after a configured endpoint returns validated PrizePicks player-prop records.