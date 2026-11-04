# Public SOP guidance collection

Reviewed September 28, 2026. The user authorized official, publicly available guidance for the anonymous demo. “Public” describes the content's intended audience; its S3 bucket, vector store and Bedrock retrieval remain access-controlled.

## Sources and scope

| Official publication | Source date | Indexed sections |
|---|---|---|
| [NPS: Firefighting Orders and Watch Out Situations](https://www.nps.gov/articles/firefighting-orders-watchout-situations.htm) | Updated August 20, 2025 | Complete 10 Standard Firefighting Orders; complete 18 Fire Watch-Out Situations |
| [FEMA Ready.gov: Wildfires](https://www.ready.gov/wildfires) | Updated August 11, 2026 | Preparation; during a wildfire; returning home |
| [FEMA Ready.gov: Evacuation](https://www.ready.gov/evacuation) | Updated March 20, 2026 | Risks/destinations/information sources; before evacuation; during evacuation |
| [USDA Forest Service: Risk Management](https://www.fs.usda.gov/managing-land/fire/safety) | No publication/update date stated | Objectives/residual risk; risk-management process |

The ten sections retain source wording and complete safety lists. HTML presentation/navigation is removed; source snapshots and SHA-256 checksums are preserved in `backend/knowledge/public-sop/`. No generated summaries become source evidence. The review date is distinct from the publisher's date; an undated source remains undated.

NWCG's publication page lists the January 2025 Incident Response Pocket Guide, but its linked PDF hostname failed DNS resolution during collection. That PDF was **not ingested**. The Forest Service's 2020 Operational Risk Management PDF was inspected for context but not indexed: its graphical matrices/forms require a separate extraction review. NPS's independently verified official checklist page supplies the firefighting orders and watch-out situations.

This initial collection does not cover every tactical SOP, communications frequency, medical protocol, local evacuation zone, current closure, shelter capacity or contraflow plan. Guidance explains considerations; it cannot validate a simulated exit or authorize deployment of a crew. Local authorities and incident command issue actual orders.

## Storage and deployment

`deployment/infra/lib/public-guidance.ts` defines a separate, retained CloudFormation stack named from account/region/deployment identity. It creates a private versioned S3 document bucket, S3 vector bucket/index, Bedrock KB service role, knowledge base and S3 data source. Existing private knowledge bases are neither copied nor modified. Generated resource names avoid global name conflicts.

An installation that owns its public-guidance stack uses `managed`: `existing` and `none` never deploy that stack, so changes to it, such as the Documents bucket's D52 Deny, would not reach it, and preflight refuses them. `managed` also provisions and ingests the collection for a new installation.

Guardrails use native `AWS::Bedrock::Guardrail` and `AWS::Bedrock::GuardrailVersion` resources in the backend's separate CloudFormation nested stack. The full deployment command manages both CDK stacks and backend templates. CDK uses `CfnKnowledgeBase` and `CfnDataSource` for the public collection; no custom resource creates the KB or guardrail. Uploading reviewed documents and running ingestion remain explicit post-provisioning deployment steps.

The index uses Titan Text Embeddings V2, 1,024 float dimensions and cosine distance. `AMAZON_BEDROCK_TEXT` is non-filterable; each custom metadata record remains under 1 KB. The data source uses `NONE` chunking because the input documents are already reviewed, bounded, complete sections. This avoids splitting the numbered safety lists. Chunking changes require a new data source; dimension/index configuration changes require a reviewed migration. The source and index have retention policies, with no implicit destructive cleanup.

The KB service role may invoke only the selected embedding model, read the `documents/` prefix and access its own vector index. Its trust policy restricts Bedrock to the selected account and region. The KB ARN wildcard is needed for initial CloudFormation creation without a circular reference; resource permissions remain scoped to this collection. The application Runtime's existing public KB policy receives the exact KB ARN. This does not create an anonymous KB endpoint.

For a complete installation, set this in the installation file (`deployment/installation.toml`):

```toml
[knowledge]
public_guidance = "managed"
```

`public_guidance = "existing"` with `public_knowledge_base_id` binds an existing separate public KB that implements the same metadata contract instead. `"none"` leaves public retrieval unavailable. The installation file keeps the choice, so later deployments keep the managed collection bound.

`bash deployment/deploy-all.sh --apply` provisions, ingests and verifies the collection and binds it into the Runtime in the same release; source updates ship the same way. It does not modify private KBs. The former standalone `deployment/deploy_public_sop.py` command is retired, because it left the collection unbound. `--apply` also bootstraps CDK in a fresh account/region.

Before writes, validation checks AWS identity, stack ownership/status, embedding model metadata, S3 Vectors access and pinned collection checksums. Deployment captures a cloud assembly, uploads versioned text/metadata, rejects unexpected objects in the ingestion prefix, starts ingestion, waits for a clean completion, and checks three relevant retrieval queries. A KB is not reported ready merely because its stack exists. Receipts record object version IDs, SHA-256, ingestion statistics, retrieval titles/URLs/scores and the new KB ID. Similarity scores are not truth probabilities.

## Retrieval and UI

The public response specialist uses a local Strands tool and IAM `bedrock:Retrieve`, separate from Gateway's provider/simulator tools. `PUBLIC_DEMO_KNOWLEDGE_BASE_ID` is server configured. It is the Runtime's only retrieval tool: there is no private-collection path and no retrieval cache. Public queries filter `collection=wildfire-public-guidance-v1`; returned records must also have `classification=public` and an allowlisted official HTTPS source URL.

Each source section carries `title`, `publisher`, `source_url`, `source_date` when stated, `retrieved_at`, `source_id`, `collection` and `classification`. The tool returns source text, relevance and citation metadata without exposing internal S3 paths. The public specialist writes up to five concise planning applications, each referencing one to three retrieved passage IDs. Code verifies those IDs and attaches their actual source titles. Invocation-scoped collectors preserve the specialist answer and citations through final Runtime assembly; the supervisor does not rewrite them. Unsupported or empty sections yield unavailable guidance. Citation titles, URLs, dates and excerpts come from retrieval, and only sources referenced by the plan appear. Identifier validation establishes source identity; it does not establish application faithfulness or local applicability. Select **Inspect this answer’s evidence** to see that answer's official links, publisher, publication date when known and collection review date in the right drawer. Articles remain folded until opened. The drawer does not assign an invented common publication title to different sources. URLs are allowlisted again at rendering.

The prompt separates published guidance from scenario interpretation. Missing configuration, no matching reviewed records and provider failures return unavailable, with no invented replacement procedure. Retrieved text is treated as reference data, not tool instructions. Schema validation and grounded citation identities cannot guarantee every generated sentence; factual evaluation remains necessary.

## Maintenance and verification

Run `deployment/build_public_sop.py --check` before deployment. A refresh requires review of the official source, applicability/date, exact extracted text and manifest changes; it is not an unattended crawl. Do not infer that a source remains current solely because its URL responds. Recheck guidance before demonstrations and whenever a publisher revises it.

Tests cover complete numbered lists, deterministic extraction, checksum tampering, extra files, wrong-account refusal, private storage/retention/IAM, public retrieval filtering, missing sources/provider failures, and hostile citation URLs. Live acceptance must verify clean ingestion, relevant retrieval, actual public Investigation answers/citations and absence of private-source results. Each release records that outcome in its acceptance receipts ([workspace operations](29-workspace-operations.md#state-evidence-and-retries)).

Run the portable live browser check after publication (Chrome and Playwright must be installed):

```sh
WFM_PUBLIC_URL=https://YOUR_DISTRIBUTION.cloudfront.net \
PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
node deployment/acceptance/check_public_sop_browser.cjs
```

It creates a new public demo session, asks a real guidance question, opens the Sources drawer, checks link actionability, and compares every displayed citation excerpt, URL, publisher and date with the pinned collection. It saves a screenshot and receipt under `.runtime/verification/public-sop-browser/` (override with `WFM_BROWSER_OUTPUT`). It does not use operator credentials or private KB access. The model answer is evaluated for required topics, not certified as universally accurate.
