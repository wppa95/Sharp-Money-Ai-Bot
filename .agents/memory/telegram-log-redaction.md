---
name: Telegram log redaction
description: Protects workflow logs when HTTP client logger handlers bypass the application stdout handler.
---

Credential filtering must be defense in depth: redact records before handlers see them, protect the application output handler, and suppress verbose HTTP-client request lines that embed credentials in URL paths. Some managed workflow logger setups can bypass an assumed handler-level filter.

**Why:** A handler filter and logger filter passed isolated tests but live workflow logs still emitted Telegram request URLs. Disabling HTTP-client INFO request lines removed the leak while retaining warnings/errors for diagnosis.

**How to apply:** Keep external request logging at WARNING or above unless a provider supports a guaranteed sanitized formatter. Test the actual managed workflow output after restart, not only an in-process logger.