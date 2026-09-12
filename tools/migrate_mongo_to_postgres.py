# -*- coding: utf-8 -*-
"""
Script hỗ trợ chuyển đổi dữ liệu hiện có từ TinyMongo (Local Database) hoặc MongoDB sang PostgreSQL.
Chạy script:
    python tools/migrate_mongo_to_postgres.py
"""
import asyncio
import json
import os
import sys

# Thêm thư mục gốc vào sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config_loader import load_config
from utils.db import PostgresDatabase, DBModel


async def migrate_local_database(pg_db: PostgresDatabase, local_db_dir: str = "./local_database"):
    if not os.path.exists(local_db_dir):
        print(f"[INFO] Không tìm thấy thư mục {local_db_dir}, bỏ qua migrate local database.")
        return

    print(f"[INFO] Bắt đầu quét dữ liệu trong {local_db_dir}...")
    total_migrated = 0

    for collection_name in os.listdir(local_db_dir):
        col_dir = os.path.join(local_db_dir, collection_name)
        if not os.path.isdir(col_dir):
            continue

        for file_name in os.listdir(col_dir):
            if not file_name.endswith(".json"):
                continue

            db_name = file_name[:-5]
            file_path = os.path.join(col_dir, file_name)

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = json.load(f)

                # TinyMongo lưu cấu trúc: {"_default": {1: {doc1}, 2: {doc2}}}
                docs = []
                if isinstance(content, dict):
                    for table_name, table_data in content.items():
                        if isinstance(table_data, dict):
                            docs.extend(table_data.values())
                        elif isinstance(table_data, list):
                            docs.extend(table_data)

                for doc in docs:
                    if not isinstance(doc, dict) or "_id" not in doc:
                        continue

                    doc_id = doc["_id"]
                    await pg_db.update_data(
                        id_=doc_id,
                        data=doc,
                        db_name=db_name,
                        collection=collection_name,
                    )
                    total_migrated += 1

                print(f"[OK] Đã chuyển {len(docs)} bản ghi từ {collection_name}/{file_name} sang PostgreSQL.")
            except Exception as e:
                print(f"[ERROR] Lỗi khi đọc file {file_path}: {e}")

    print(f"[HOÀN TẤT] Tổng cộng đã chuyển đổi {total_migrated} bản ghi từ Local Database.")


async def main():
    config = load_config()
    postgres_url = config.get("POSTGRES_URL") or config.get("DATABASE_URL")

    if not postgres_url:
        print("[ERROR] Chưa cấu hình POSTGRES_URL hoặc DATABASE_URL trong .env!")
        return

    print(f"[INFO] Kết nối tới PostgreSQL: {postgres_url}")
    pg_db = PostgresDatabase(postgres_url)

    try:
        await pg_db.init_tables()
        await migrate_local_database(pg_db)
    finally:
        await pg_db.close()

    print("[SUCCESS] Quá trình migration hoàn tất thành công!")


if __name__ == "__main__":
    asyncio.run(main())
