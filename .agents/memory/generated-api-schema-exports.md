---
name: Generated API schema exports
description: Orval behavior affecting the shared Zod package index
---

When an operation has both path and query parameters, the current Orval output can emit the same `*Params` name in generated API Zod and generated TypeScript types. Re-export the generated API schemas explicitly while exporting generated types separately, omitting the colliding path schemas from the package index.

**Why:** Re-exporting both generated modules with `export *` fails the workspace `isolatedModules` typecheck even though the generated files themselves are valid.

**How to apply:** After changing the OpenAPI spec, run library typechecking and inspect the package index; do not expose the colliding path-parameter symbols through `@workspace/api-zod`.