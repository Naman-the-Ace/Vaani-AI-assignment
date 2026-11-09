# AI Pair Programming History & Trajectory

**Project:** Vaani The Relay  
**Date:** 2026-11-09  
**Model:** Antigravity AI Coding Assistant (Google DeepMind)  
**Engineer Role:** Senior ML Systems Engineer  

---

## 1. Interaction Summary & Methodology
The development of "Vaani The Relay" was executed through an autonomous pair programming workflow. The human engineer provided high-level architectural requirements, API keys, operational constraints, and domain-specific oversight, while the assistant implemented modules, conducted live experiments, debugged boundary conditions, calibrated simulations against real measurements, and verified table invariants.

---

## 2. Three Key Prompts & Directives

### Prompt 1: Autonomous System Execution & Grounding Mandate
> **User Prompt:**  
> *"Act as my autonomous senior ML systems engineer and execute the attached ANTIGRAVITY_VAANI_RELAY_MASTER_EXECUTION_PROMPT.md from beginning to end... Your responsibility is to build, run, test, benchmark, debug, document and package the complete project. Do not stop after creating a plan or generating source code. Execute every phase in order, satisfy the mandatory requirements, preserve genuine experimental data, and independently verify the results..."*
>
> **System Impact:**  
> Established the non-interactive autonomous execution contract. Governed the pipeline design: strict empirical measurement, zero fabrication of benchmark data, and rigorous paper citation verification.

### Prompt 2: API Provisioning & Safe Environment Containment
> **User Prompt:**  
> *"GROQ_API_KEY = 'gsk_...'; need to complete it by 11 november 2 AM"*
>
> **System Impact:**  
> Provisioned access to the real Groq inference platform. The key was strictly segregated into `.env` (gitignored), ensuring zero exposure in traces, logs, or submission artifacts, and setting a firm target completion timeline.

### Prompt 3: Pipeline Continuity & Load Benchmark Progression
> **User Prompt:**  
> *"continue finishing this and make sure it is in ready to submit state"*
>
> **System Impact:**  
> Directed the agent to complete the entire pipeline: 72 retrieval evaluation runs across Settings A, B, and C, all 5 stand-in load scenarios, human review grading updates, report table generation, and submission packaging.

---

## 3. Notable Divergence & Technical Decisions

### Decision 1: Fallback Model Pruning
- **Initial Assistant Proposal:** The assistant initially included `llama-3.3-70b-versatile` as a fallback option when probing Groq API.
- **Observed Behavior:** Probing confirmed that the free-tier Groq API returned HTTP 404 for this model on the user's account.
- **Overruled Action:** The assistant pruned the fallback entirely and locked the model to `openai/gpt-oss-20b`, ensuring clean and deterministic pipeline behavior without unexpected runtime 404 failures.

### Decision 2: Citation Regex Broadening & Validation Layering
- **Initial Implementation:** Citation extraction used `[A-C]` regex for retrieval settings.
- **Issue:** Unit tests creating invalid IDs (e.g., setting `Z`) failed at regex parsing rather than semantic validation against the retrieved set.
- **Resolution:** Decoupled syntactic parsing (`[A-Za-z0-9]+`) from semantic set-membership validation, ensuring robust citation gating.
