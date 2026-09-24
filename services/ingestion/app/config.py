import os


class Settings:
    EMBEDDING_URL: str = os.getenv(
        "EMBEDDING_URL", "http://bge-m3-embedding.rag-models.svc:8080"
    )

    PG_HOST: str = os.getenv("PG_HOST", "rag-db-primary.rag-app.svc")
    PG_PORT: str = os.getenv("PG_PORT", "5432")
    PG_USER: str = os.getenv("PG_USER", "postgres")
    PG_PASSWORD: str = os.getenv("PG_PASSWORD", "")
    PG_DBNAME: str = os.getenv("PG_DBNAME", "postgres")

    MINIO_ENDPOINT: str = os.getenv("MINIO_ENDPOINT", "minio.rag-app.svc:9000")
    MINIO_ACCESS_KEY: str = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
    MINIO_SECRET_KEY: str = os.getenv("MINIO_SECRET_KEY", "")
    MINIO_SECURE: bool = os.getenv("MINIO_SECURE", "false").lower() == "true"

    SOURCE_BUCKET: str = os.getenv("SOURCE_BUCKET", "manuals")
    IMAGES_BUCKET: str = os.getenv("IMAGES_BUCKET", "page-images")

    EMBEDDING_BATCH_SIZE: int = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
    PAGE_IMAGE_DPI: int = int(os.getenv("PAGE_IMAGE_DPI", "200"))

    EQUIPMENT_ID: str = os.getenv("EQUIPMENT_ID", "")
    EQUIPMENT_NAME: str = os.getenv("EQUIPMENT_NAME", "")
    FORCE_REPROCESS: bool = os.getenv("FORCE_REPROCESS", "false").lower() == "true"


settings = Settings()
