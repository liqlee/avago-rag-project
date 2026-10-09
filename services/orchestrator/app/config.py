import os
from urllib.parse import quote_plus


class Settings:
    VLLM_BASE_URL: str = os.getenv("VLLM_BASE_URL", "http://vllm-qwen.rag-models.svc:8000")
    EMBEDDING_URL: str = os.getenv("EMBEDDING_URL", "http://bge-m3-embedding.rag-models.svc:8080")
    RERANKER_URL: str = os.getenv("RERANKER_URL", "http://bge-reranker.rag-models.svc:8080")
    GUARDIAN_URL: str = os.getenv("GUARDIAN_URL", "http://guardian.rag-models.svc:8080")

    PG_HOST: str = os.getenv("PG_HOST", "rag-db-primary.rag-app.svc")
    PG_PORT: str = os.getenv("PG_PORT", "5432")
    PG_USER: str = os.getenv("PG_USER", "postgres")
    PG_PASSWORD: str = os.getenv("PG_PASSWORD", "")
    PG_DBNAME: str = os.getenv("PG_DBNAME", "rag-db")

    RETRIEVAL_TOP_K: int = int(os.getenv("RETRIEVAL_TOP_K", "20"))
    RERANK_TOP_N: int = int(os.getenv("RERANK_TOP_N", "5"))
    GUARDIAN_ENABLED: bool = os.getenv("GUARDIAN_ENABLED", "true").lower() == "true"
    QUERY_REWRITE_ENABLED: bool = os.getenv("QUERY_REWRITE_ENABLED", "true").lower() == "true"
    MODEL_NAME: str = os.getenv("MODEL_NAME", "qwen-7b")

    def get_database_url(self) -> str:
        return (
            f"postgresql://{quote_plus(self.PG_USER)}:{quote_plus(self.PG_PASSWORD)}"
            f"@{self.PG_HOST}:{self.PG_PORT}/{self.PG_DBNAME}"
        )


settings = Settings()
