# Adapting Muhafiz to New Domains

Muhafiz (itself a pivot from an earlier tax-domain build, TaxIQ) was built with a highly modular architecture that cleanly separates the core RAG pipeline from domain-specific data and rules. By design, you can transform this system into another domain entirely — a different law-enforcement/public-safety client, or an unrelated vertical — with minimal structural changes.

This guide outlines exactly what needs to be swapped out and what components should remain untouched when adapting the codebase for a new domain.

## 🟢 What Stays the Same (Core Architecture)

The majority of the infrastructure is domain-agnostic and should **not** be modified:

1. **Authentication & Security (`src/auth/`)**: 
   - JWT generation, password hashing, and role-based access control (RBAC).
2. **Data Gateway & Persistence (`src/data_gateway/`)**: 
   - The dual `RestGateway` and `DirectGateway` patterns. 
   - The SQLite pipeline logger (`data/pipeline_logs.db`).
3. **Core Orchestration (`src/pipeline/orchestrator.py`)**: 
   - The central loop (Rewrite → Route → Retrieve → Rerank → Evaluate → Generate).
4. **Retrieval Mechanisms (`src/retrieval/`)**:
   - The `ChromaDB` vector store implementation.
   - The `BM25` lexical search algorithm.
   - The Reciprocal Rank Fusion (RRF) logic.
5. **Project & Memory Management (`src/api/projects.py`, `src/pipeline/memory_updater.py`)**:
   - The rolling summary generation and project-scoped conversational memory.

## 🔴 What Needs to Change (Domain-Specifics)

To adapt the system to a new domain, you will primarily update prompts, data schemas, and ingestion logic.

### 1. System Prompts (`prompts/`)
The LLM behavior is entirely controlled by text files in the `prompts/` directory. You must rewrite these to reflect the new domain:
- `query_rewriter.txt`: Update examples to reflect domain-specific terminology (e.g., changing "filer status" to "patient symptoms").
- `router.txt`: Update the few-shot routing examples. Define clearly what constitutes a "RAG" query vs a "DIRECT" conversational query in the new domain.
- `evaluator.txt`: Update the criteria for what makes a retrieved document "relevant" to a query.
- `final_response.txt`: Modify the persona (e.g., "You are an expert Tax Assistant" -> "You are an expert Medical Assistant").

### 2. Structured Data & MCP Tools (`src/mcp/`)
The current SQL route is hardcoded to query a `police_reference_data` table.
- **Database Schema**: Drop the `police_reference_data` table in PostgreSQL and create new structured tables relevant to your domain (e.g., `drug_interactions`, `case_law_metadata`).
- **MCP Client (`src/mcp/client.py`)**: Update the `execute_query` connection parameters if pointing to a different structured database.
- **SQL Extractor (`src/pipeline/sql_extractor.py`)**: Update the prompt (`sql_param_extractor.txt`) to extract the fields relevant to your new tables (e.g., extracting `drug_name` and `dosage` instead of `category` and `section_ref`).

### 3. Ingestion Pipeline (`src/ingestion/`)
If your new domain requires parsing different file formats (e.g., DICOM images for healthcare, specific XML schemas for legal tech), you must add new loaders:
- Extend `src/ingestion/loaders/` with custom parsers.
- Update `src/ingestion/service.py` to route the new file extensions to your custom loaders.

### 4. Database Models (`src/database/models.py`)
While the `users`, `projects`, and `sessions` tables remain the same, you may want to add custom tables for domain-specific user preferences (e.g., replacing `preferred_language` with `medical_license_number` in the user profile).

## Summary Checklist for a New Domain

- [ ] Clear the ChromaDB vector store (`rm -rf data/chroma_db`).
- [ ] Drop and recreate domain-specific Postgres tables (replace `police_reference_data`).
- [ ] Rewrite all 5 `.txt` prompts in the `prompts/` folder.
- [ ] Update `sql_extractor.py` and its prompt to target the new SQL tables.
- [ ] Ingest the new domain's raw documents.
- [ ] (Optional) Update frontend branding and terminology in `frontend/src/`.
