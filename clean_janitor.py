#!/usr/bin/env python3
import os
import time
import logging
from pathlib import Path

DOWNLOAD_DIR = Path("/root/downloader_bot/downloads")
MAX_AGE_SECONDS = 3600  # 1 hour threshold

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] CleanJanitor: %(message)s"
)
logger = logging.getLogger("CleanJanitor")

def clean_stale_downloads():
    if not DOWNLOAD_DIR.exists():
        return

    now = time.time()
    removed_count = 0
    reclaimed_bytes = 0

    for file_path in DOWNLOAD_DIR.iterdir():
        if not file_path.is_file():
            continue

        # Skip gitkeep or hidden system files
        if file_path.name.startswith("."):
            continue

        try:
            mtime = file_path.stat().st_mtime
            age = now - mtime

            # Only remove files older than 1 hour (strictly prevents deleting active in-flight downloads)
            if age > MAX_AGE_SECONDS:
                size = file_path.stat().st_size
                file_path.unlink()
                removed_count += 1
                reclaimed_bytes += size
                logger.info(f"Removed stale file: {file_path.name} (Age: {int(age/60)}m, Size: {size/1024/1024:.2f}MB)")
        except Exception as e:
            logger.warning(f"Could not check/remove {file_path.name}: {e}")

    if removed_count > 0:
        logger.info(f"Cleanup finished: {removed_count} files removed, {reclaimed_bytes/1024/1024:.2f}MB reclaimed.")
    else:
        logger.info("Cleanup finished: No stale files found.")

if __name__ == "__main__":
    clean_stale_downloads()
