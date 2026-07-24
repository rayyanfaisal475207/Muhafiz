import os
import subprocess

def run(cmd):
    print(f"Running: {cmd}")
    subprocess.run(cmd, shell=True, check=False)

commits = [
    ("Initial project scaffolding and configuration", [".gitignore", "pytest.ini", "requirements.txt", ".env.example", "README.md", "RUN.md", "docker-compose.yml", "test.md"]),
    ("Set up database connection and core models", ["src/database/"]),
    ("Implement Alembic migrations structure", ["alembic.ini", "alembic/", "migrations/"]),
    ("Add basic FastAPI application entry point", ["src/main.py", "src/config.py"]),
    ("Implement authentication and JWT logic", ["src/auth/"]),
    ("Add project and case management API endpoints", ["src/api/cases.py", "src/api/projects.py", "src/api/case_assignments.py"]),
    ("Add profile, sessions, attachments API endpoints", ["src/api/profile.py", "src/api/sessions.py", "src/api/attachments.py"]),
    ("Add admin API endpoint", ["src/api/admin.py", "src/api/graph_review.py"]),
    ("Implement direct data gateways", ["src/data_gateway/"]),
    ("Set up LLM client and integrations", ["src/llm/"]),
    ("Add document loaders", ["src/ingestion/loaders/"]),
    ("Implement chunking and ingestion service", ["src/ingestion/"]),
    ("Add semantic retrieval and embedder", ["src/retrieval/embedder.py", "src/retrieval/vector_store.py", "src/retrieval/cross_reranker.py"]),
    ("Implement keyword and BM25 retrieval", ["src/retrieval/bm25_retriever.py"]),
    ("Add web search and graph retrieval capabilities", ["src/retrieval/web_search.py", "src/retrieval/graph_retriever.py", "src/retrieval/cross_reranker.py"]),
    ("Implement query rewriter and expander", ["src/pipeline/query_rewriter.py", "src/pipeline/query_expander.py"]),
    ("Add query router and SQL extractor", ["src/pipeline/router.py", "src/pipeline/sql_extractor.py"]),
    ("Implement pipeline orchestrator and evaluator", ["src/pipeline/orchestrator.py", "src/pipeline/evaluator.py", "src/pipeline/title_generator.py"]),
    ("Implement verifier agent and memory updater", ["src/pipeline/verifier.py", "src/pipeline/memory_updater.py"]),
    ("Add file structurer and document generation", ["src/pipeline/file_structurer.py", "src/generation/"]),
    ("Implement conversation memory", ["src/memory/"]),
    ("Add entity extraction and graph rules", ["src/extraction/", "src/graph/", "src/pipeline/xagg.py"]),
    ("Add observability and analytics", ["src/observability/"]),
    ("Set up frontend configuration and scaffolding", ["frontend/package.json", "frontend/package-lock.json", "frontend/vite.config.ts", "frontend/tsconfig*.json", "frontend/tailwind.config.js", "frontend/postcss.config.js", "frontend/index.html"]),
    ("Implement frontend core components and layout", ["frontend/src/index.css", "frontend/src/App.tsx", "frontend/src/main.tsx", "frontend/src/components/layout/", "frontend/src/components/brand/"]),
    ("Add chat interface and messaging UI", ["frontend/src/components/chat/", "frontend/src/pages/"]),
    ("Integrate frontend stores and API utilities", ["frontend/src/store/", "frontend/src/lib/", "frontend/src/utils.ts", "frontend/src/constants.ts", "frontend/public/", "frontend/src/icons.svg", "frontend/src/favicon.svg"]),
    ("Add admin frontend scaffolding", ["admin-frontend/"]),
    ("Add system prompts", ["prompts/"]),
    ("Add tests for backend logic", ["tests/"]),
    ("Add helper scripts and utilities", ["scripts/"]),
    ("Add comprehensive architecture and design docs", ["docs/", "PHASE*.md", "SOW*.pdf", "check_db.py", "src/api/__init__.py", "src/__init__.py", "src/pipeline/__init__.py", "src/retrieval/__init__.py"]),
    ("Final catch-all for remaining files", ["."])
]

# Ensure everything is tracked or untracked properly
for msg, files in commits:
    for f in files:
        run(f"git add {f}")
    # Only commit if there is something staged
    status = subprocess.run("git status --porcelain", shell=True, capture_output=True, text=True)
    if status.stdout.strip():
        run(f'git commit -m "{msg}"')
