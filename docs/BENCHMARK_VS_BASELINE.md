# Retrieval benchmark — code-memory vs no-code-memory baseline

**Corpus**: `acme/sample-webapp` (anonymized)  
**Project slug**: `sample-webapp`  
**Queries**: `scripts/benchmark_queries.json` (30 hand-crafted Angular queries — natural-language + identifier mix)  
**Baseline (no code-memory)**: ripgrep keyword search over the working tree — what an agent falls back to when no semantic index exists.  
**code-memory**: bi-encoder (`bge-m3` dense via Ollama) + cross-encoder rerank (`bge-reranker-v2-m3`, α=0.5).  
**Hardware**: Apple Silicon (MPS), fp16  

## Takeaways

- **Best MRR**: `code-memory (dense only)` at 0.798
- **Best nDCG@10**: `code-memory (dense only)` at 0.840
- **Best Recall@10**: `code-memory (dense only)` at 0.967

## Summary

| Mode | Recall@5 | Recall@10 | MRR | nDCG@10 | p50 (ms) | p95 (ms) |
|------|---------:|----------:|----:|--------:|---------:|---------:|
| grep (no code-memory) | 0.367 | 0.367 | 0.169 | 0.218 | 175.9 | 327.2 |
| code-memory (dense only) | 0.967 | 0.967 | 0.798 | 0.840 | 85.8 | 1935.2 |
| code-memory (dense+rerank) | 0.900 | 0.967 | 0.573 | 0.672 | 7854.6 | 14607.9 |

## Grep baseline vs code-memory (full)

| Metric | grep | code-memory | Δ |
|--------|--------:|---------:|--:|
| Recall@5  | 0.367  | 0.900  | +145.5% |
| Recall@10 | 0.367 | 0.967 | +163.6% |
| MRR       | 0.169          | 0.573          | +238.4% |
| nDCG@10   | 0.218   | 0.672   | +208.1% |
| p50 (ms)  | 175.9       | 7854.6       | +4365.9% |
| p95 (ms)  | 327.2       | 14607.9       | +4365.1% |

## Per-query results

### `how does the app fetch the configuration on startup`

Gold: ['app-config.service.ts', 'configuration-guard.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (168.3 ms)
  - top5: ['/repo/sample-webapp/AGENTS.md', '/repo/sample-webapp/CHANGELOG.md', '/repo/sample-webapp/src/app/core/shared/infrastructure/paginated-request/README.md', '/repo/sample-webapp/src/main.ts', '/repo/sample-webapp/CLAUDE.md']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (270.8 ms)
  - top5: ['/repo/sample-webapp/src/app/app-config.service.ts', '/repo/sample-webapp/src/app/core/configuration/configuration-guard.service.ts', '/repo/sample-webapp/src/app/runtime-app-config.provider.spec.ts', '/repo/sample-webapp/src/app/app-config.service.ts', '/repo/sample-webapp/src/app/app-config.service.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (8024.1 ms)
  - top5: ['/repo/sample-webapp/src/app/sales/customers/presentation/create/professional/setup-mode/index.ts', '/repo/sample-webapp/src/app/app-config.service.ts', '/repo/sample-webapp/src/app/runtime-app-config.provider.spec.ts', '/repo/sample-webapp/src/app/core/configuration/configuration-guard.service.ts', '/repo/sample-webapp/src/app/app-config.service.ts']

### `logout flow that clears stored user state`

Gold: ['logout-store-clearer.service.ts', 'logout-redirect.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (349.5 ms)
  - top5: ['/repo/sample-webapp/AGENTS.md', '/repo/sample-webapp/src/app/direct-debits/presentation/create/steps/due-date/direct-debits-due-date.component.spec.ts', '/repo/sample-webapp/src/app/direct-debits/presentation/create/steps/due-date/direct-debits-due-date.component.ts', '/repo/sample-webapp/CLAUDE.md', '/repo/sample-webapp/README.md']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (1533.2 ms)
  - top5: ['/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/logout-cache-clearer.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (7053.3 ms)
  - top5: ['/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/index.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/logout-cache-clearer.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts']

### `where is the navigation breadcrumb built`

Gold: ['breadcrumb.service.ts', 'subheader-builder.service.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (180.1 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md', '/repo/sample-webapp/src/app/app.routes.ts', '/repo/sample-webapp/src/app/direct-debits/application/facades/direct-debits.facade.ts', '/repo/sample-webapp/src/app/direct-debits/presentation/routes.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (1628.0 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (8836.4 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.spec.ts', '/repo/sample-webapp/src/app/navigation/presentation/breadcrumb/index.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/navigation/application/models/index.ts']

### `service that calls the dunning letter REST API`

Gold: ['dunning-letter.service.ts', 'dunning.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (297.5 ms)
  - top5: ['/repo/sample-webapp/AGENTS.md', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md', '/repo/sample-webapp/docs/refactoring/legacy-modules-migration-plan.md', '/repo/sample-webapp/CHANGELOG.md', '/repo/sample-webapp/CLAUDE.md']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (154.4 ms)
  - top5: ['/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.spec.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning.service.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.ts', '/repo/sample-webapp/src/domains/dunning/data-access/adapters-api/dunning-letter-api.adapter.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (8968.8 ms)
  - top5: ['/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.spec.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning.service.ts']

### `VAT facade exposed to components`

Gold: ['vat.facade.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (183.4 ms)
  - top5: ['/repo/sample-webapp/AGENTS.md', '/repo/sample-webapp/README.md', '/repo/sample-webapp/docs/CODEMAPS/APPLICATION.md', '/repo/sample-webapp/docs/CODEMAPS/ARCHITECTURE.md', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md']
- **code-memory (dense only)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (85.6 ms)
  - top5: ['/repo/sample-webapp/src/app/taxes/presentation/forms/vat-application/vat-application-form.component.spec.ts', '/repo/sample-webapp/src/app/taxes/application/facades/vats.facade.ts', '/repo/sample-webapp/src/app/taxes/presentation/forms/vat-application/vat-application-form.component.ts', '/repo/sample-webapp/src/app/taxes/presentation/forms/vat-application/vat-application-form.component.ts', '/repo/sample-webapp/src/app/taxes/presentation/forms/vat-application/vat-application-form.component.ts']
- **code-memory (dense+rerank)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (10156.3 ms)
  - top5: ['/repo/sample-webapp/src/app/taxes/presentation/forms/vat-application/vat-application-form.component.spec.ts', '/repo/sample-webapp/src/app/taxes/presentation/forms/vat-application/vat-application-form.component.ts', '/repo/sample-webapp/src/app/taxes/presentation/forms/application-parameters/application-parameters-form.component.spec.ts', '/repo/sample-webapp/src/app/taxes/application/facades/vats.facade.ts', '/repo/sample-webapp/src/app/taxes/presentation/list/taxes-list.component.spec.ts']

### `fiscal year repository fetching periods`

Gold: ['fiscal-year.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (308.9 ms)
  - top5: ['/repo/sample-webapp/README.md', '/repo/sample-webapp/docs/CODEMAPS/AREAS.md', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md', '/repo/sample-webapp/docs/Source_code_management.md', '/repo/sample-webapp/docs/refactoring/legacy-modules-migration-plan.md']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=0.33 ndcg=0.50 (500.3 ms)
  - top5: ['/repo/sample-webapp/src/domains/fiscal-year/models/fixture/fiscal-year.fixture.ts', '/repo/sample-webapp/src/domains/business-review/utils/periodicity.helper.spec.ts', '/repo/sample-webapp/src/domains/fiscal-year/data-access/services/fiscal-year.service.ts', '/repo/sample-webapp/src/domains/fiscal-year/data-access/services/fiscal-year.service.spec.ts', '/repo/sample-webapp/src/domains/business-review/utils/periodicity.helper.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.20 ndcg=0.39 (8303.4 ms)
  - top5: ['/repo/sample-webapp/src/domains/fiscal-year/models/index.ts', '/repo/sample-webapp/src/domains/business-review/utils/periodicity.helper.spec.ts', '/repo/sample-webapp/src/domains/fiscal-year/data-access/services/fiscal-year.service.spec.ts', '/repo/sample-webapp/src/domains/fiscal-year/models/fixture/fiscal-year.fixture.ts', '/repo/sample-webapp/src/domains/fiscal-year/data-access/services/fiscal-year.service.ts']

### `warehouse data access service`

Gold: ['warehouse.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (180.6 ms)
  - top5: ['/repo/sample-webapp/docs/CODEMAPS/AREAS.md', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md', '/repo/sample-webapp/docs/refactoring/legacy-modules-migration-plan.md', '/repo/sample-webapp/src/app/pages/business-review/business-review/business-review.component.spec.ts', '/repo/sample-webapp/src/app/pages/business-review/business-review/business-review.component.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (2281.3 ms)
  - top5: ['/repo/sample-webapp/src/domains/warehouse/data-access/index.ts', '/repo/sample-webapp/src/domains/warehouse/data-access/services/warehouse.service.ts', '/repo/sample-webapp/src/domains/warehouse/data-access/services/warehouse.service.spec.ts', '/repo/sample-webapp/src/domains/warehouse/data-access/services/warehouse.service.ts', '/repo/sample-webapp/src/domains/business-review/data-access/services/business-review.service.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.33 ndcg=0.50 (5384.6 ms)
  - top5: ['/repo/sample-webapp/src/domains/warehouse/data-access/services/warehouse.service.spec.ts', '/repo/sample-webapp/src/domains/warehouse/data-access/index.ts', '/repo/sample-webapp/src/domains/warehouse/data-access/services/warehouse.service.ts', '/repo/sample-webapp/src/domains/due-date/data-access/index.ts', '/repo/sample-webapp/src/domains/warehouse/data-access/adapters/warehouse.adapter.spec.ts']

### `currencies application facade`

Gold: ['currencies.facade.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (159.5 ms)
  - top5: ['/repo/sample-webapp/docs/refactoring/legacy-modules-migration-plan.md', '/repo/sample-webapp/src/app/core/sales/commissions/commissions-services.providers.ts', '/repo/sample-webapp/src/app/currencies/currencies.providers.ts', '/repo/sample-webapp/src/app/deb/deb-services.providers.ts', '/repo/sample-webapp/src/app/deb/presentation/declaration/deb-declaration-documents-overview/deb-declaration-documents-overview.component.spec.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (85.2 ms)
  - top5: ['/repo/sample-webapp/src/app/currencies/application/currencies.facade.ts', '/repo/sample-webapp/src/app/currencies/application/index.ts', '/repo/sample-webapp/src/app/deb/application/facades/deb.facade.ts', '/repo/sample-webapp/src/app/deb/application/facades/deb.facade.ts', '/repo/sample-webapp/src/app/core/sales/commissions/application/facades/commissions.facade.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (9290.3 ms)
  - top5: ['/repo/sample-webapp/src/app/currencies/application/index.ts', '/repo/sample-webapp/src/app/currencies/application/currencies.facade.ts', '/repo/sample-webapp/src/app/core/sales/commissions/application/facades/commissions.facade.ts', '/repo/sample-webapp/src/app/deb/application/facades/deb.facade.ts', '/repo/sample-webapp/src/app/deb/application/facades/deb.facade.ts']

### `user navigation persistence in local storage`

Gold: ['user-local-storage.service.ts', 'user-navigation.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (278.0 ms)
  - top5: ['/repo/sample-webapp/src/app/core/guards/user-default-params/user-default-params.guard.spec.ts', '/repo/sample-webapp/src/app/core/guards/user-default-params/user-default-params.guard.ts', '/repo/sample-webapp/src/app/pages/invalid-config/invalid-config.component.ts', '/repo/sample-webapp/src/domains/user/data-access/index.ts', '/repo/sample-webapp/AGENTS.md']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=0.20 ndcg=0.39 (1652.1 ms)
  - top5: ['/repo/sample-webapp/src/domains/user/data-access/index.ts', '/repo/sample-webapp/a11y/run-pa11y-auth.mjs', '/repo/sample-webapp/src/app/core/guards/user-default-params/user-default-params.guard.ts', '/repo/sample-webapp/src/app/core/guards/user-default-params/user-default-params.guard.ts', '/repo/sample-webapp/src/domains/user/data-access/services/user-local-storage.service.ts']
- **code-memory (dense+rerank)** — r@5=0 r@10=1 rr=0.17 ndcg=0.36 (6169.7 ms)
  - top5: ['/repo/sample-webapp/src/domains/user/data-access/index.ts', '/repo/sample-webapp/src/app/core/guards/user-default-params/user-default-params.guard.ts', '/repo/sample-webapp/src/domains/user/data-access/services/user-local-storage.service.spec.ts', '/repo/sample-webapp/a11y/run-pa11y-auth.mjs', '/repo/sample-webapp/src/app/core/guards/user-default-params/user-default-params.guard.ts']

### `company domain data access service`

Gold: ['company.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (237.3 ms)
  - top5: ['/repo/sample-webapp/docs/CODEMAPS/AREAS.md', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md', '/repo/sample-webapp/docs/refactoring/legacy-modules-migration-plan.md', '/repo/sample-webapp/src/app/deb/presentation/declaration/deb-declaration/deb-declaration.component.spec.ts', '/repo/sample-webapp/src/app/deb/presentation/declaration/deb-declaration/deb-declaration.component.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (73.9 ms)
  - top5: ['/repo/sample-webapp/src/domains/company/data-access/index.ts', '/repo/sample-webapp/src/domains/company/data-access/services/company.service.ts', '/repo/sample-webapp/src/domains/company/data-access/services/company.service.ts', '/repo/sample-webapp/src/domains/company/data-access/services/company.service.ts', '/repo/sample-webapp/src/domains/company/data-access/services/company.service.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.33 ndcg=0.50 (5797.4 ms)
  - top5: ['/repo/sample-webapp/src/domains/company/data-access/index.ts', '/repo/sample-webapp/src/domains/company/data-access/services/company.service.spec.ts', '/repo/sample-webapp/src/domains/company/data-access/services/company.service.ts', '/repo/sample-webapp/src/domains/company/data-access/fixtures/company-api.fixture.ts', '/repo/sample-webapp/src/domains/company/data-access/services/company.service.ts']

### `due date computation service`

Gold: ['due-date.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (190.3 ms)
  - top5: ['/repo/sample-webapp/AGENTS.md', '/repo/sample-webapp/CHANGELOG.md', '/repo/sample-webapp/docs/CODEMAPS/APPLICATION.md', '/repo/sample-webapp/docs/CODEMAPS/AREAS.md', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=0.25 ndcg=0.43 (1301.2 ms)
  - top5: ['/repo/sample-webapp/src/domains/dunning/data-access/adapters/dunning-due-date.adapter.spec.ts', '/repo/sample-webapp/src/shared/api/data-access/IS-GC_Legacy_V1/model/dueDateApi.model.ts', '/repo/sample-webapp/src/domains/dunning/data-access/adapters/dunning-due-date.adapter.ts', '/repo/sample-webapp/src/domains/due-date/data-access/services/due-date.service.ts', '/repo/sample-webapp/src/domains/dunning/ui/dunning-due-date-list/sort-dunning-due-dates.pipe.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (9239.8 ms)
  - top5: ['/repo/sample-webapp/src/domains/due-date/data-access/services/due-date.service.ts', '/repo/sample-webapp/src/domains/due-date/data-access/index.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning.service.ts', '/repo/sample-webapp/src/domains/dunning/data-access/adapters/dunning-due-date.adapter.ts', '/repo/sample-webapp/src/domains/dunning/data-access/adapters/dunning-due-date.adapter.spec.ts']

### `user store for the user domain`

Gold: ['user-store.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (185.7 ms)
  - top5: ['/repo/sample-webapp/AGENTS.md', '/repo/sample-webapp/README.md', '/repo/sample-webapp/docs/CODEMAPS/AREAS.md', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md', '/repo/sample-webapp/docs/CODEMAPS/STATE.md']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (71.5 ms)
  - top5: ['/repo/sample-webapp/src/domains/user/data-access/store/user-store.service.ts', '/repo/sample-webapp/src/domains/user/data-access/services/user-local-storage.service.ts', '/repo/sample-webapp/src/domains/user/data-access/index.ts', '/repo/sample-webapp/src/app/core/shared/user/application/store/index.ts', '/repo/sample-webapp/src/app/core/guards/user-default-params/user-default-params.guard.ts']
- **code-memory (dense+rerank)** — r@5=0 r@10=1 rr=0.17 ndcg=0.36 (13971.1 ms)
  - top5: ['/repo/sample-webapp/src/domains/user/data-access/index.ts', '/repo/sample-webapp/src/domains/user/models/index.ts', '/repo/sample-webapp/src/domains/user/data-access/services/user-local-storage.service.ts', '/repo/sample-webapp/src/app/core/guards/user-default-params/user-default-params.guard.ts', '/repo/sample-webapp/src/app/core/shared/user/application/store/index.ts']

### `dunning letter edit facade`

Gold: ['dunning-letter-edit.facade.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=0.25 ndcg=0.43 (237.5 ms)
  - top5: ['/repo/sample-webapp/docs/CODEMAPS/APPLICATION.md', '/repo/sample-webapp/docs/CODEMAPS/STATE.md', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.component.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.facade.ts', '/repo/sample-webapp/AGENTS.md']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (81.7 ms)
  - top5: ['/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.facade.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.component.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.component.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.component.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.facade.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (6649.0 ms)
  - top5: ['/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.facade.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.component.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.component.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.component.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit-filter/dunning-letter-edit-filter.component.ts']

### `monthly treatment accounting service`

Gold: ['monthly-treatment.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (172.8 ms)
  - top5: ['/repo/sample-webapp/docs/CODEMAPS/AREAS.md', '/repo/sample-webapp/docs/CODEMAPS/FILES.md', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md', '/repo/sample-webapp/docs/CODEMAPS/ROUTING.md', '/repo/sample-webapp/docs/refactoring/legacy-modules-migration-plan.md']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (1232.5 ms)
  - top5: ['/repo/sample-webapp/src/domains/accounting/data-access/services/monthly-treatment.service.ts', '/repo/sample-webapp/src/domains/accounting/data-access/index.ts', '/repo/sample-webapp/src/domains/accounting/ui/index.ts', '/repo/sample-webapp/src/domains/accounting/data-access/stores/monthly-treatment/models/monthly-treatment-key.constant.ts', '/repo/sample-webapp/src/domains/accounting/data-access/stores/monthly-treatment/monthly-treatment-store.module.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (9010.8 ms)
  - top5: ['/repo/sample-webapp/src/domains/accounting/data-access/index.ts', '/repo/sample-webapp/src/domains/accounting/data-access/services/monthly-treatment.service.ts', '/repo/sample-webapp/src/domains/accounting/data-access/services/monthly-treatment.service.spec.ts', '/repo/sample-webapp/src/domains/accounting/data-access/stores/monthly-treatment/effects/monthly-treatment.effects.ts', '/repo/sample-webapp/src/domains/accounting/data-access/services/monthly-treatment.service.ts']

### `report categories service`

Gold: ['report-category.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (116.6 ms)
  - top5: ['/repo/sample-webapp/docs/CODEMAPS/MODULES.md', '/repo/sample-webapp/docs/CODEMAPS/STATE.md', '/repo/sample-webapp/src/app/pages/accounting/monthly-treatment/monthly-visualization/monthly-visualization.component.spec.ts', '/repo/sample-webapp/src/app/pages/accounting/monthly-treatment/monthly-visualization/monthly-visualization.component.ts', '/repo/sample-webapp/src/app/pages/accounting/monthly-treatment/monthly-visualization/services/stimulsoft-launcher.service.spec.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (83.6 ms)
  - top5: ['/repo/sample-webapp/src/domains/report/data-access/services/report-category.service.ts', '/repo/sample-webapp/src/domains/report/data-access/services/report-category.service.ts', '/repo/sample-webapp/src/domains/report/models/fixtures/report-categories.fixture.ts', '/repo/sample-webapp/src/domains/report/data-access/services/report-category.service.ts', '/repo/sample-webapp/src/app/pages/report/reports-list/reports-list.component.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (6734.4 ms)
  - top5: ['/repo/sample-webapp/src/domains/report/data-access/services/report-category.service.ts', '/repo/sample-webapp/src/domains/report/data-access/services/report-category.service.ts', '/repo/sample-webapp/src/domains/report/data-access/services/report-category.service.spec.ts', '/repo/sample-webapp/src/domains/report/models/fixtures/report-categories.fixture.ts', '/repo/sample-webapp/src/app/pages/report/reports-list/reports-list.component.ts']

### `navigation hook that intercepts route changes`

Gold: ['navigation-hook.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (286.6 ms)
  - top5: ['/repo/sample-webapp/AGENTS.md', '/repo/sample-webapp/src/app/sales/customers/presentation/create/shared/base/base-step.component.ts', '/repo/sample-webapp/src/app/sales/orders/presentation/orders-list/orders-list.component.spec.ts', '/repo/sample-webapp/CLAUDE.md', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.spec.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (73.2 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.ts', '/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.ts', '/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (7685.1 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.ts', '/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts']

### `demo mode login listener for auth`

Gold: ['demo-mode-login-listener.service.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=0.25 ndcg=0.43 (302.7 ms)
  - top5: ['/repo/sample-webapp/src/app/app.config.ts', '/repo/sample-webapp/src/app/app.routes.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.spec.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.ts', '/repo/sample-webapp/AGENTS.md']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (83.9 ms)
  - top5: ['/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.spec.ts', '/repo/sample-webapp/src/app/core/auth/index.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.ts', '/repo/sample-webapp/src/shared/api/authentication/data-access/services/demo-mode.service.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (10822.3 ms)
  - top5: ['/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.ts', '/repo/sample-webapp/src/app/core/auth/index.ts', '/repo/sample-webapp/src/shared/api/authentication/data-access/index.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.spec.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.ts']

### `business review domain form service`

Gold: ['business-review-form.service.ts', 'business-review.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (234.2 ms)
  - top5: ['/repo/sample-webapp/AGENTS.md', '/repo/sample-webapp/docs/CODEMAPS/AREAS.md', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md', '/repo/sample-webapp/docs/refactoring/legacy-modules-migration-plan.md', '/repo/sample-webapp/tsconfig.json']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (1000.1 ms)
  - top5: ['/repo/sample-webapp/src/domains/business-review/feature/services/business-review-form.service.ts', '/repo/sample-webapp/src/domains/business-review/feature/services/business-review-form.service.ts', '/repo/sample-webapp/src/domains/business-review/feature/services/business-review-form.service.ts', '/repo/sample-webapp/src/domains/business-review/feature/services/business-review-form.service.ts', '/repo/sample-webapp/src/domains/business-review/feature/services/business-review-form.service.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (7513.0 ms)
  - top5: ['/repo/sample-webapp/src/domains/business-review/feature/services/business-review-form.service.ts', '/repo/sample-webapp/src/domains/business-review/feature/companies-step/companies-step.component.ts', '/repo/sample-webapp/src/domains/business-review/feature/services/business-review-form.service.ts', '/repo/sample-webapp/src/domains/business-review/feature/services/business-review-form.service.ts', '/repo/sample-webapp/src/app/pages/business-review/business-review/business-review.component.ts']

### `product characteristics data service`

Gold: ['product-characteristics.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (175.9 ms)
  - top5: ['/repo/sample-webapp/src/app/pages/product/parameter/code-structure/code-structure.component.html', '/repo/sample-webapp/src/app/pages/product/parameter/code-structure/code-structure.component.spec.ts', '/repo/sample-webapp/src/app/pages/product/parameter/code-structure/code-structure.component.ts', '/repo/sample-webapp/src/domains/accounting/feature/monthly-treatment/sales-by-product-category-treatment-id-retriever-card/sales-by-product-category-treatment-id-retriever-card.component.html', '/repo/sample-webapp/src/domains/accounting/feature/monthly-treatment/sales-by-product-category-treatment-id-retriever-card/sales-by-product-category-treatment-id-retriever-card.component.spec.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (85.3 ms)
  - top5: ['/repo/sample-webapp/src/domains/product/data-access/service/product-characteristics.service.ts', '/repo/sample-webapp/src/domains/product/data-access/index.ts', '/repo/sample-webapp/src/domains/product/data-access/service/product-designation.service.ts', '/repo/sample-webapp/src/domains/product/data-access/service/product-characteristics.service.ts', '/repo/sample-webapp/src/domains/product/data-access/stores/product-characteristics/models/product-characteristic.model.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (8194.0 ms)
  - top5: ['/repo/sample-webapp/src/domains/product/data-access/index.ts', '/repo/sample-webapp/src/domains/product/data-access/service/product-characteristics.service.ts', '/repo/sample-webapp/src/domains/product/data-access/service/product-characteristics.service.spec.ts', '/repo/sample-webapp/src/domains/product/data-access/stores/code-structure/product-characteristic-selection.service.ts', '/repo/sample-webapp/src/domains/product/data-access/stores/code-structure/product-characteristics-fixed.service.ts']

### `custom report information service`

Gold: ['custom-report-information.service.ts', 'report-information.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (175.9 ms)
  - top5: ['/repo/sample-webapp/AGENTS.md', '/repo/sample-webapp/docs/CODEMAPS/ROUTING.md', '/repo/sample-webapp/src/app/pages/report/reports-list/reports-list.component.spec.ts', '/repo/sample-webapp/src/app/pages/report/reports-list/reports-list.component.ts', '/repo/sample-webapp/src/assets/i18n/fr-FR.json']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (77.7 ms)
  - top5: ['/repo/sample-webapp/src/domains/report/models/fixtures/custom-report-information.fixture.ts', '/repo/sample-webapp/src/domains/report/data-access/services/custom-report-information.service.ts', '/repo/sample-webapp/src/domains/report/data-access/services/custom-report-information.service.ts', '/repo/sample-webapp/src/domains/report/data-access/fixtures/custom-report-information-api-fixture.ts', '/repo/sample-webapp/src/domains/report/data-access/index.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (7161.8 ms)
  - top5: ['/repo/sample-webapp/src/domains/report/data-access/index.ts', '/repo/sample-webapp/src/domains/report/data-access/services/custom-report-information.service.ts', '/repo/sample-webapp/src/domains/report/models/fixtures/custom-report-information.fixture.ts', '/repo/sample-webapp/src/domains/report/data-access/services/custom-report-information.service.spec.ts', '/repo/sample-webapp/src/domains/report/data-access/store/effects/report.effects.ts']

### `BreadcrumbService`

Gold: ['breadcrumb.service.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (55.6 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/navigation/presentation/breadcrumb/breadcrumb.component.ts', '/repo/sample-webapp/src/app/navigation/presentation/layout/page/page.component.ts', '/repo/sample-webapp/src/app/sales/customers/presentation/create/professional/setup-mode/mobile/setup-mode.component.spec.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=0.33 ndcg=0.50 (76.9 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.spec.ts', '/repo/sample-webapp/src/app/navigation/presentation/breadcrumb/breadcrumb.component.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/navigation/presentation/breadcrumb/breadcrumb.component.ts', '/repo/sample-webapp/src/app/navigation/presentation/breadcrumb/index.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.33 ndcg=0.50 (7373.6 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.spec.ts', '/repo/sample-webapp/src/app/navigation/presentation/breadcrumb/breadcrumb.component.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/navigation/presentation/breadcrumb/breadcrumb.component.ts', '/repo/sample-webapp/src/app/navigation/presentation/breadcrumb/index.ts']

### `DunningLetterService`

Gold: ['dunning-letter.service.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (55.7 ms)
  - top5: ['/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.spec.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.ts', '/repo/sample-webapp/src/domains/dunning/data-access/stores/dunning-parameters/effects/dunning-parameters.effects.spec.ts', '/repo/sample-webapp/src/domains/dunning/data-access/stores/dunning-parameters/effects/dunning-parameters.effects.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (1336.2 ms)
  - top5: ['/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.ts', '/repo/sample-webapp/src/shared/api/data-access/IS-GC_Legacy_V1/model/dunningLetterResponseApi.model.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.spec.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.facade.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (9199.6 ms)
  - top5: ['/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.spec.ts', '/repo/sample-webapp/src/domains/dunning/data-access/services/dunning-letter.service.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit/dunning-letter-edit.component.ts', '/repo/sample-webapp/src/domains/dunning/feature/dunning-letter-edit-filter/dunning-letter-edit-filter.component.ts']

### `FiscalYearService`

Gold: ['fiscal-year.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (56.1 ms)
  - top5: ['/repo/sample-webapp/src/app/pages/business-review/business-review/business-review.component.spec.ts', '/repo/sample-webapp/src/domains/business-review/data-access/services/business-review.service.spec.ts', '/repo/sample-webapp/src/domains/business-review/data-access/services/business-review.service.ts', '/repo/sample-webapp/src/domains/business-review/feature/advanced-settings/advanced-settings.component.spec.ts', '/repo/sample-webapp/src/domains/business-review/feature/companies-step/companies-step.component.spec.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=0.33 ndcg=0.50 (82.5 ms)
  - top5: ['/repo/sample-webapp/src/domains/fiscal-year/data-access/services/fiscal-year.service.spec.ts', '/repo/sample-webapp/src/domains/fiscal-year/data-access/index.ts', '/repo/sample-webapp/src/domains/fiscal-year/data-access/services/fiscal-year.service.ts', '/repo/sample-webapp/src/domains/business-review/feature/services/business-review-form.service.ts', '/repo/sample-webapp/src/domains/fiscal-year/data-access/services/fiscal-year.service.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (6656.0 ms)
  - top5: ['/repo/sample-webapp/src/domains/fiscal-year/data-access/services/fiscal-year.service.spec.ts', '/repo/sample-webapp/src/domains/fiscal-year/data-access/services/fiscal-year.service.ts', '/repo/sample-webapp/src/domains/fiscal-year/data-access/index.ts', '/repo/sample-webapp/src/domains/business-review/data-access/services/business-review.service.spec.ts', '/repo/sample-webapp/src/domains/business-review/data-access/services/business-review.service.ts']

### `UserStoreService`

Gold: ['user-store.service.ts']
- **grep (no code-memory)** — r@5=0 r@10=0 rr=0.00 ndcg=0.00 (55.5 ms)
  - top5: ['/repo/sample-webapp/src/app/core/guards/user-default-params/user-default-params.guard.spec.ts', '/repo/sample-webapp/src/app/core/guards/user-default-params/user-default-params.guard.ts', '/repo/sample-webapp/src/app/core/shared/user/infrastructure/adapter/get-company-adapter.service.ts', '/repo/sample-webapp/src/app/core/shared/user/infrastructure/adapter/get-user-session-adapter.service.ts', '/repo/sample-webapp/src/app/pages/accounting/monthly-treatment/monthly-treatment.component.spec.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (136.3 ms)
  - top5: ['/repo/sample-webapp/src/domains/user/data-access/store/user-store.service.ts', '/repo/sample-webapp/src/domains/user/data-access/store/user-store.service.spec.ts', '/repo/sample-webapp/src/domains/user/data-access/index.ts', '/repo/sample-webapp/src/domains/user/data-access/services/user-local-storage.service.ts', '/repo/sample-webapp/src/domains/user/data-access/services/user-local-storage.service.spec.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.33 ndcg=0.50 (7322.7 ms)
  - top5: ['/repo/sample-webapp/src/domains/user/data-access/index.ts', '/repo/sample-webapp/src/domains/user/data-access/store/user-store.service.spec.ts', '/repo/sample-webapp/src/domains/user/data-access/store/user-store.service.ts', '/repo/sample-webapp/src/domains/user/data-access/services/user-local-storage.service.ts', '/repo/sample-webapp/src/app/core/shared/user/infrastructure/adapter/get-company-adapter.service.ts']

### `AppConfigService`

Gold: ['app-config.service.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=0.25 ndcg=0.43 (56.7 ms)
  - top5: ['/repo/sample-webapp/AGENTS.md', '/repo/sample-webapp/docs/CODEMAPS/MODULES.md', '/repo/sample-webapp/src/app/app-config.service.spec.ts', '/repo/sample-webapp/src/app/app-config.service.ts', '/repo/sample-webapp/src/app/chat/infrastructure/adapters/progress-stream.adapter.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (84.7 ms)
  - top5: ['/repo/sample-webapp/src/app/app-config.service.ts', '/repo/sample-webapp/src/app/core/navigation/feature/services/flag.service.spec.ts', '/repo/sample-webapp/src/app/core/navigation/feature/services/flag.service.ts', '/repo/sample-webapp/src/app/app-config.service.spec.ts', '/repo/sample-webapp/src/app/core/configuration/configuration-guard.service.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (8973.4 ms)
  - top5: ['/repo/sample-webapp/src/app/app-config.service.ts', '/repo/sample-webapp/src/app/core/navigation/feature/services/flag.service.spec.ts', '/repo/sample-webapp/src/app/core/navigation/feature/services/flag.service.ts', '/repo/sample-webapp/src/app/app-config.service.spec.ts', '/repo/sample-webapp/src/app/core/configuration/configuration-guard.service.ts']

### `VatFacade`

Gold: ['vat.facade.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (54.8 ms)
  - top5: ['/repo/sample-webapp/src/app/vat/application/index.ts', '/repo/sample-webapp/src/app/vat/application/vat.facade.ts', '/repo/sample-webapp/src/app/vat/vat.providers.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (81.1 ms)
  - top5: ['/repo/sample-webapp/src/app/vat/application/vat.facade.ts', '/repo/sample-webapp/src/app/taxes/application/facades/vats.facade.ts', '/repo/sample-webapp/src/app/vat/application/index.ts', '/repo/sample-webapp/src/app/taxes/application/facades/vat-situations.facade.ts', '/repo/sample-webapp/src/app/taxes/application/facades/taxes-form.facade.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (15386.2 ms)
  - top5: ['/repo/sample-webapp/src/app/vat/application/index.ts', '/repo/sample-webapp/src/app/vat/application/vat.facade.ts', '/repo/sample-webapp/src/app/taxes/application/facades/taxes-form.facade.ts', '/repo/sample-webapp/src/app/taxes/application/facades/vats.facade.ts', '/repo/sample-webapp/src/app/taxes/application/facades/index.ts']

### `CurrenciesFacade`

Gold: ['currencies.facade.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (56.3 ms)
  - top5: ['/repo/sample-webapp/src/app/currencies/application/currencies.facade.ts', '/repo/sample-webapp/src/app/currencies/application/index.ts', '/repo/sample-webapp/src/app/currencies/currencies.providers.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (1632.1 ms)
  - top5: ['/repo/sample-webapp/src/app/currencies/application/currencies.facade.ts', '/repo/sample-webapp/src/app/currencies/application/index.ts', '/repo/sample-webapp/src/app/direct-debits/application/facades/direct-debits.facade.ts', '/repo/sample-webapp/src/app/sales/customers/application/facades/customer-orders.facade.ts', '/repo/sample-webapp/src/app/deb/application/facades/deb.facade.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (2477.8 ms)
  - top5: ['/repo/sample-webapp/src/app/currencies/application/index.ts', '/repo/sample-webapp/src/app/currencies/application/currencies.facade.ts', '/repo/sample-webapp/src/app/core/sales/commissions/application/facades/commissions.facade.ts', '/repo/sample-webapp/src/app/currencies/application/store/index.ts', '/repo/sample-webapp/src/app/direct-debits/application/facades/direct-debits.facade.ts']

### `LogoutStoreClearerService`

Gold: ['logout-store-clearer.service.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=0.25 ndcg=0.43 (60.3 ms)
  - top5: ['/repo/sample-webapp/src/app/app.config.ts', '/repo/sample-webapp/src/app/core/auth/index.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.spec.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (85.9 ms)
  - top5: ['/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/logout-cache-clearer.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/index.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (7219.0 ms)
  - top5: ['/repo/sample-webapp/src/app/core/auth/index.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/logout-store-clearer.service.ts', '/repo/sample-webapp/src/app/core/auth/logout-cache-clearer.ts']

### `NavigationHookService`

Gold: ['navigation-hook.service.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=0.25 ndcg=0.43 (58.7 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.ts', '/repo/sample-webapp/src/app/sales/customers/presentation/create/shared/base/base-step.component.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (334.3 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/core/navigation/feature/services/navigation.service.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.33 ndcg=0.50 (8369.0 ms)
  - top5: ['/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.spec.ts', '/repo/sample-webapp/src/app/navigation/application/navigation-hook.service.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts', '/repo/sample-webapp/src/app/navigation/application/breadcrumb.service.ts']

### `DemoModeLoginListenerService`

Gold: ['demo-mode-login-listener.service.ts']
- **grep (no code-memory)** — r@5=1 r@10=1 rr=0.33 ndcg=0.50 (57.2 ms)
  - top5: ['/repo/sample-webapp/src/app/app.config.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.spec.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.ts', '/repo/sample-webapp/src/app/core/auth/index.ts']
- **code-memory (dense only)** — r@5=1 r@10=1 rr=1.00 ndcg=1.00 (75.0 ms)
  - top5: ['/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.ts', '/repo/sample-webapp/src/app/core/auth/index.ts', '/repo/sample-webapp/src/shared/api/authentication/data-access/services/demo-mode.service.ts', '/repo/sample-webapp/src/app/pages/dunning/dunning-parameters/can-deactivate-dunning-parameters.spec.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.spec.ts']
- **code-memory (dense+rerank)** — r@5=1 r@10=1 rr=0.50 ndcg=0.63 (7567.8 ms)
  - top5: ['/repo/sample-webapp/src/app/core/auth/index.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.ts', '/repo/sample-webapp/src/shared/api/authentication/data-access/index.ts', '/repo/sample-webapp/src/app/core/auth/demo-mode-login-listener.service.ts', '/repo/sample-webapp/src/shared/api/authentication/data-access/services/demo-mode.service.ts']
