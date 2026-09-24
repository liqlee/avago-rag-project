"""Docling ingestion pipeline entry point.

Processes all unprocessed PDFs from the MinIO manuals bucket:
  1. Download PDF from MinIO
  2. Convert via Docling (OCR + layout analysis)
  3. Extract page images via PyMuPDF
  4. Upload page images to MinIO
  5. Chunk by document structure with classification
  6. Batch embed via BGE-M3
  7. Store chunks + vectors in pgvector
  8. Track progress in ingestion_logs
"""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import psycopg2
from minio import Minio

from .chunker import chunk_document
from .config import settings
from .embedder import embed_batch
from .ingest import convert_pdf, extract_page_images

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def get_minio_client() -> Minio:
    return Minio(
        endpoint=settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
    )


def get_pg_conn():
    return psycopg2.connect(
        host=settings.PG_HOST,
        port=settings.PG_PORT,
        user=settings.PG_USER,
        password=settings.PG_PASSWORD,
        dbname=settings.PG_DBNAME,
    )


def ensure_buckets(mc: Minio):
    for bucket in (settings.SOURCE_BUCKET, settings.IMAGES_BUCKET):
        if not mc.bucket_exists(bucket):
            mc.make_bucket(bucket)
            logger.info("Created bucket: %s", bucket)


def get_processed_files(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute("SELECT file_name FROM ingestion_logs WHERE status = 'completed'")
    result = {row[0] for row in cur.fetchall()}
    cur.close()
    return result


def create_log(conn, file_name: str, file_path: str) -> str:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ingestion_logs (file_name, file_path, status, started_at)
        VALUES (%s, %s, 'processing', NOW())
        RETURNING id
        """,
        (file_name, file_path),
    )
    conn.commit()
    log_id = str(cur.fetchone()[0])
    cur.close()
    return log_id


def update_log(
    conn, log_id: str, status: str, chunks_created: int = 0, error: str | None = None
):
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE ingestion_logs
        SET status = %s,
            chunks_created = %s,
            error_message = %s,
            completed_at = CASE
                WHEN %s IN ('completed', 'failed') THEN NOW()
                ELSE completed_at
            END
        WHERE id = %s::uuid
        """,
        (status, chunks_created, error, status, log_id),
    )
    conn.commit()
    cur.close()


def store_chunks(conn, chunks, vectors):
    cur = conn.cursor()
    for chunk, vector in zip(chunks, vectors):
        vector_str = f"[{','.join(str(x) for x in vector)}]"
        cur.execute(
            """
            INSERT INTO chunks (id, text, chunk_type, metadata, dense_vector)
            VALUES (%s, %s, %s, %s, %s::vector)
            ON CONFLICT (id) DO UPDATE SET
                text = EXCLUDED.text,
                chunk_type = EXCLUDED.chunk_type,
                metadata = EXCLUDED.metadata,
                dense_vector = EXCLUDED.dense_vector,
                updated_at = NOW()
            """,
            (
                chunk.id,
                chunk.text,
                chunk.chunk_type,
                json.dumps(chunk.metadata),
                vector_str,
            ),
        )
    conn.commit()
    cur.close()


def upload_page_images(
    mc: Minio, local_paths: list[str], file_stem: str
) -> list[str]:
    urls: list[str] = []
    for path in local_paths:
        object_name = f"{file_stem}/{Path(path).name}"
        mc.fput_object(settings.IMAGES_BUCKET, object_name, path)
        urls.append(f"s3://{settings.IMAGES_BUCKET}/{object_name}")
    return urls


def process_one(mc: Minio, conn, file_name: str):
    """Process a single PDF end-to-end."""
    file_stem = Path(file_name).stem
    logger.info("Processing: %s", file_name)
    log_id = create_log(conn, file_name, f"s3://{settings.SOURCE_BUCKET}/{file_name}")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Download from MinIO
            local_pdf = str(Path(tmpdir) / Path(file_name).name)
            mc.fget_object(settings.SOURCE_BUCKET, file_name, local_pdf)
            logger.info("  Downloaded: %s", file_name)

            # 2. Extract page images
            img_dir = str(Path(tmpdir) / "images")
            os.makedirs(img_dir)
            image_paths = extract_page_images(local_pdf, img_dir)
            logger.info("  Extracted %d page images", len(image_paths))

            # 3. Upload page images to MinIO
            image_urls = upload_page_images(mc, image_paths, file_stem)
            logger.info("  Uploaded page images to MinIO")

            # 4. Convert with Docling
            logger.info("  Running Docling (OCR + layout analysis)...")
            doc = convert_pdf(local_pdf)

            # 5. Chunk
            manual_title = file_stem.replace("_", " ").replace("-", " ")
            chunks = chunk_document(
                doc,
                file_stem,
                manual_title,
                settings.EQUIPMENT_ID,
                settings.EQUIPMENT_NAME,
                image_urls,
            )
            logger.info("  Created %d chunks", len(chunks))

            if not chunks:
                update_log(conn, log_id, "completed", 0)
                logger.warning("  No chunks created (document may be empty or too short)")
                return

            # 6. Embed
            logger.info("  Embedding %d chunks...", len(chunks))
            vectors = embed_batch([c.text for c in chunks])

            # 7. Store in pgvector
            store_chunks(conn, chunks, vectors)
            logger.info("  Stored %d chunks in pgvector", len(chunks))

            update_log(conn, log_id, "completed", len(chunks))
            logger.info("  Done: %s → %d chunks", file_name, len(chunks))

    except Exception as e:
        logger.error("  Failed: %s — %s", file_name, e, exc_info=True)
        update_log(conn, log_id, "failed", error=str(e)[:500])


def main():
    logger.info("=" * 60)
    logger.info("  Docling Ingestion Pipeline")
    logger.info("=" * 60)

    mc = get_minio_client()
    conn = get_pg_conn()

    ensure_buckets(mc)

    # List PDFs in source bucket
    objects = list(mc.list_objects(settings.SOURCE_BUCKET, recursive=True))
    pdf_files = [
        obj.object_name
        for obj in objects
        if obj.object_name.lower().endswith(".pdf")
    ]

    if not pdf_files:
        logger.info(
            "No PDFs found in bucket '%s'. "
            "Upload PDFs via MinIO console, then re-run this job.",
            settings.SOURCE_BUCKET,
        )
        conn.close()
        return

    logger.info("Found %d PDFs in '%s'", len(pdf_files), settings.SOURCE_BUCKET)

    # Filter already-processed files
    if settings.FORCE_REPROCESS:
        to_process = pdf_files
        logger.info("Force reprocess — will process all %d files", len(to_process))
    else:
        processed = get_processed_files(conn)
        to_process = [f for f in pdf_files if f not in processed]
        skipped = len(pdf_files) - len(to_process)
        if skipped:
            logger.info("Skipping %d already-processed files", skipped)
        logger.info("%d files to process", len(to_process))

    if not to_process:
        logger.info("Nothing to process. Done.")
        conn.close()
        return

    success = 0
    failed = 0
    for file_name in to_process:
        try:
            process_one(mc, conn, file_name)
            success += 1
        except Exception:
            failed += 1

    conn.close()

    logger.info("=" * 60)
    logger.info("  Complete: %d succeeded, %d failed", success, failed)
    logger.info("=" * 60)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
