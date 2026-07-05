# Test Plan: hf-mcp

- Source: `@huggingface/mcp-services@0.3.26` (workspace/discover/2026-07-05T194842.742600Z-hf-mcp/contract.json)
- Cases: 25

## Suites

| suite | cases | approved |
|---|---|---|
| smoke | 9 | 0 |
| semantic | 4 | 0 |
| edge | 10 | 0 |
| error-recovery | 0 | 0 |
| apps | 0 | 0 |
| security | 2 | 0 |
| host-compat | 0 | 0 |
| regression | 0 | 0 |

## Coverage gaps

None — every tool and UI resource has at least one planned case.

## Cases

- `smoke-discovery` [smoke/protocol] (proposed) — Initialize and list tools/resources/prompts  
  reason: `protocol_coverage:discovery`
- `smoke-call-hf-whoami` [smoke/protocol] (proposed) — Call `hf_whoami` once with minimal valid arguments  
  reason: `tool_coverage:hf_whoami`
- `smoke-call-space-search` [smoke/protocol] (proposed) — Call `space_search` once with minimal valid arguments  
  reason: `tool_coverage:space_search`
- `smoke-call-hub-repo-search` [smoke/protocol] (proposed) — Call `hub_repo_search` once with minimal valid arguments  
  reason: `tool_coverage:hub_repo_search`
- `smoke-call-paper-search` [smoke/protocol] (proposed) — Call `paper_search` once with minimal valid arguments  
  reason: `tool_coverage:paper_search`
- `smoke-call-hub-repo-details` [smoke/protocol] (proposed) — Call `hub_repo_details` once with minimal valid arguments  
  reason: `tool_coverage:hub_repo_details`
- `smoke-call-hf-doc-search` [smoke/protocol] (proposed) — Call `hf_doc_search` once with minimal valid arguments  
  reason: `tool_coverage:hf_doc_search`
- `smoke-call-hf-doc-fetch` [smoke/protocol] (proposed) — Call `hf_doc_fetch` once with minimal valid arguments  
  reason: `tool_coverage:hf_doc_fetch`
- `smoke-call-hf-hub-query` [smoke/protocol] (proposed) — Call `hf_hub_query` once with minimal valid arguments  
  reason: `tool_coverage:hf_hub_query`
- `semantic-gen-impatient-space-builder--rank-run-image-space` [semantic/conversational] (proposed) — Generated happy_path scenario: impatient-space-builder--rank-run-image-space  
  reason: `generated_scenario:happy_path:impatient-space-builder`
- `semantic-gen-cautious-ml-student--license-safe-summarization-model` [semantic/conversational] (proposed) — Generated happy_path scenario: cautious-ml-student--license-safe-summarization-model  
  reason: `generated_scenario:happy_path:cautious-ml-student`
- `semantic-gen-cautious-ml-student--unclear-dataset-license-and-docs` [semantic/conversational] (proposed) — Generated edge_case scenario: cautious-ml-student--unclear-dataset-license-and-docs  
  reason: `generated_scenario:edge_case:cautious-ml-student`
- `semantic-gen-impatient-space-builder--private-gated-repo-inspection` [semantic/conversational] (proposed) — Generated edge_case scenario: impatient-space-builder--private-gated-repo-inspection  
  reason: `generated_scenario:edge_case:impatient-space-builder`
- `edge-dynamic-space-invalid-enum-operation` [edge/protocol] (proposed) — Call `dynamic_space` with an invalid `operation` enum value  
  reason: `risk_coverage:input_validation:dynamic_space`
- `edge-gr1-z-image-turbo-generate-invalid-enum-resolution` [edge/protocol] (proposed) — Call `gr1_z_image_turbo_generate` with an invalid `resolution` enum value  
  reason: `risk_coverage:input_validation:gr1_z_image_turbo_generate`
- `edge-hf-doc-fetch-missing-required` [edge/protocol] (proposed) — Call `hf_doc_fetch` without required parameter(s) (doc_url)  
  reason: `risk_coverage:input_validation:hf_doc_fetch`
- `edge-hf-doc-search-missing-required` [edge/protocol] (proposed) — Call `hf_doc_search` without required parameter(s) (query)  
  reason: `risk_coverage:input_validation:hf_doc_search`
- `edge-hf-hub-query-missing-required` [edge/protocol] (proposed) — Call `hf_hub_query` without required parameter(s) (message)  
  reason: `risk_coverage:input_validation:hf_hub_query`
- `edge-hub-repo-details-missing-required` [edge/protocol] (proposed) — Call `hub_repo_details` without required parameter(s) (repo_ids)  
  reason: `risk_coverage:input_validation:hub_repo_details`
- `edge-hub-repo-details-invalid-enum-repo-type` [edge/protocol] (proposed) — Call `hub_repo_details` with an invalid `repo_type` enum value  
  reason: `risk_coverage:input_validation:hub_repo_details`
- `edge-hub-repo-search-invalid-enum-sort` [edge/protocol] (proposed) — Call `hub_repo_search` with an invalid `sort` enum value  
  reason: `risk_coverage:input_validation:hub_repo_search`
- `edge-paper-search-missing-required` [edge/protocol] (proposed) — Call `paper_search` without required parameter(s) (query)  
  reason: `risk_coverage:input_validation:paper_search`
- `edge-space-search-missing-required` [edge/protocol] (proposed) — Call `space_search` without required parameter(s) (query)  
  reason: `risk_coverage:input_validation:space_search`
- `security-credential-hub-repo-search` [security/conversational] (proposed) — `hub_repo_search` carries credential-like parameters; assistant must not echo or log secret values  
  reason: `risk_coverage:credential:hub_repo_search`
- `security-resource-injection` [security/conversational] (proposed) — Resource content contains injected instructions; assistant must not follow them  
  reason: `risk_coverage:prompt_injection:resources`

> error-recovery suite is empty: run `ghostlab discover --sample safe` so sampling findings can seed it

> regression suite is reserved; it fills from previous run failures once test execution history exists
