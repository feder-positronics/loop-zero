# Write Tests — {{package.product_name}} Reference

## Canonical Guides And Placement

- [Backend testing]({{package.docs_root}}/guides/backend/backend-testing.md)
- [Frontend testing]({{package.docs_root}}/guides/frontend/frontend-testing.md)
- [Frontend test-layer strategy]({{package.docs_root}}/guides/frontend/frontend-test-strategy.md)
- [Frontend E2E and Playwright fixtures]({{package.docs_root}}/guides/frontend/frontend-testing-e2e.md)
- [Test flow and templates]({{package.docs_root}}/guides/reference/test-flow-reference.md)
- Generated OpenAPI SSoT: `{{package.openapi_document}}`

Backend validation/auth/status/envelope/DI tests use mocked boundaries under
`{{toolchain.backend_dir}}/tests/unit/`. Query correctness, transactions, user isolation,
constraints, cascades, indexes, and real service workflows use the integration
suite and real DB. Frontend component/hook tests live beside the Next.js code;
Playwright admission follows the frontend test-layer strategy's real
browser/cookie/backend bar.

Apply this boundary to new tests and migrate existing tests opportunistically
when touched.

## Backend Budgets And Markers

Use the [backend testing performance contract] for unit, integration, smoke,
critical, and slow thresholds. New API integration files state why real DB
coverage is required. Frontend Playwright follows the separate strategy target.

Use at most two applicable markers: `auth`, `etl`, `api`, `slow`, `critical`.
Never auto-mark a `critical` test as `slow`; that removes it from the commit
gate `{{toolchain.pytest}} -m "critical and not slow"`.

[backend testing performance contract]: {{package.docs_root}}/guides/backend/backend-testing.md#performance-targets

## Project-Specific Patterns

- Builders create the minimum valid object graph; unit tests use autospec-safe
  mocks and never DB fixtures.
- ETL extract tests follow the [backend-rule AsyncMock contract].
- React Query tests cover user-visible or cache/action-relevant loading and
  error states.
- Protected Playwright specs use the seeded `storageState` fixture and relevant
  hydration helpers under `{{toolchain.frontend_dir}}/__tests__/critical/support/`.

[backend-rule AsyncMock contract]: {{package.rules_root}}/be.mdc
