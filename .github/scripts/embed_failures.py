import os
import sys
import chromadb
from google import genai

GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
CHROMA_API_KEY  = os.environ.get("CHROMA_API_KEY", "")
CHROMA_TENANT   = os.environ.get("CHROMA_TENANT", "")
CHROMA_DATABASE = os.environ.get("CHROMA_DATABASE", "default_database")
COLLECTION_NAME = "ci_failures"
EMBED_MODEL     = "gemini-embedding-001"

if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY not set"); sys.exit(1)
if not CHROMA_API_KEY:
    print("ERROR: CHROMA_API_KEY not set"); sys.exit(1)
if not CHROMA_TENANT:
    print("ERROR: CHROMA_TENANT not set"); sys.exit(1)

FAILURES = [
    {
        "id": "fail_001",
        "document": "GitHub Actions pipeline failed at docker-build step. Error: COPY failed: file not found in build context. Root cause: .dockerignore was excluding the src/ directory. Fix: remove src/ from .dockerignore and rebuild the image.",
        "metadata": {"category": "docker", "severity": "medium"}
    },
    {
        "id": "fail_002",
        "document": "Terraform apply failed: Error acquiring the state lock. LockID present in S3 backend. Root cause: previous terraform apply process was killed mid-run leaving a stale lock. Fix: run terraform force-unlock with the LockID shown in the error output.",
        "metadata": {"category": "terraform", "severity": "high"}
    },
    {
        "id": "fail_003",
        "document": "ECS task failed to start: CannotPullContainerError. ECR image exists but task cannot pull it. Root cause: ECS task running in private subnet, VPC has no NAT gateway configured. Fix: add NAT gateway to private subnet route table or move task to public subnet with auto-assign public IP.",
        "metadata": {"category": "aws-ecs", "severity": "critical"}
    },
    {
        "id": "fail_004",
        "document": "pytest suite failed with ModuleNotFoundError for all project modules. Tests pass locally but fail in CI. Root cause: CI pipeline running pytest without activating the virtual environment first. Fix: add source .venv/bin/activate or use .venv/bin/pytest directly in the pipeline YAML.",
        "metadata": {"category": "python", "severity": "medium"}
    },
    {
        "id": "fail_005",
        "document": "Node.js build failed: npm ERR ERESOLVE unable to resolve dependency tree. Root cause: peer dependency conflict between react version 18 and react-testing-library version 12 which requires react 17. Fix: upgrade react-testing-library to version 13 or add --legacy-peer-deps flag to npm install.",
        "metadata": {"category": "nodejs", "severity": "medium"}
    },
    {
        "id": "fail_006",
        "document": "GitHub Actions step failed: Error Resource not accessible by integration. Automated PR creation via GitHub API returns 403. Root cause: workflow GITHUB_TOKEN does not have write permissions for pull-requests scope. Fix: add permissions block with pull-requests write to the workflow YAML file.",
        "metadata": {"category": "github-actions", "severity": "low"}
    },
    {
        "id": "fail_007",
        "document": "Integration tests failed in CI: connection refused to postgres on localhost port 5432. Root cause: GitHub Actions service container for postgres is not yet healthy when the test step begins. Fix: add health check configuration to the postgres service and add a depends-on or wait step before running tests.",
        "metadata": {"category": "database", "severity": "high"}
    },
    {
        "id": "fail_008",
        "document": "CloudFormation stack creation failed: ValidationError template format error. Stack creation rolls back immediately. Root cause: tab characters mixed with spaces in the CloudFormation YAML template. Fix: convert all tab characters to spaces throughout the template and run yamllint to validate before committing.",
        "metadata": {"category": "cloudformation", "severity": "medium"}
    },
    {
        "id": "fail_009",
        "document": "Kubernetes deployment rollout stuck with zero of three replicas ready. All pods show ImagePullBackOff status. ECR image exists and tag is correct. Root cause: pod spec is missing imagePullSecrets and cluster node IAM role does not have ECR read permissions. Fix: attach AmazonEC2ContainerRegistryReadOnly policy to the node group IAM role.",
        "metadata": {"category": "kubernetes", "severity": "critical"}
    },
    {
        "id": "fail_010",
        "document": "Lambda function deployment failed via Terraform: InvalidParameterValueException. Root cause: reserved_concurrent_executions is set to 0 in the Terraform resource which disables the Lambda function entirely. Fix: set reserved_concurrent_executions to -1 for unreserved concurrency or set a positive integer value.",
        "metadata": {"category": "aws-lambda", "severity": "low"}
    },
]

# ── Gemini embedding ──────────────────────────────────────────────────────────

gemini = genai.Client(api_key=GEMINI_API_KEY)

def get_embedding(text: str) -> list:
    result = gemini.models.embed_content(
        model=EMBED_MODEL,
        contents=text,
    )
    # The modern SDK returns an array of embeddings under `embeddings`
    return list(result.embeddings[0].values)

# ── ChromaDB Cloud ────────────────────────────────────────────────────────────

print(f"Connecting to ChromaDB Cloud (tenant={CHROMA_TENANT}, db={CHROMA_DATABASE})...")

client = chromadb.HttpClient(
    ssl=True,
    host="api.trychroma.com",
    tenant=CHROMA_TENANT,
    database=CHROMA_DATABASE,
    headers={"x-chroma-token": CHROMA_API_KEY},
)

collection = client.get_or_create_collection(
    name=COLLECTION_NAME,
    metadata={"hnsw:space": "cosine"},
)

print(f"Collection '{COLLECTION_NAME}' ready. Embedding {len(FAILURES)} failures...\n")

for failure in FAILURES:
    embedding = get_embedding(failure["document"])
    collection.upsert(
        ids=[failure["id"]],
        embeddings=[embedding],
        documents=[failure["document"]],
        metadatas=[failure["metadata"]],
    )
    print(f"  ✓ {failure['id']} — {failure['metadata']['category']} ({failure['metadata']['severity']})")

print(f"\n✅ {len(FAILURES)} CI failure patterns stored in ChromaDB Cloud")
print(f"   Collection : {COLLECTION_NAME}")
print(f"   Tenant     : {CHROMA_TENANT}")
print(f"   Database   : {CHROMA_DATABASE}")
print(f"   Embed model: {EMBED_MODEL}")
