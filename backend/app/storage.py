"""
Shared Supabase Storage access -- a service-role client used to read files
out of the `medical-documents` bucket, which is (correctly) private, after
a caller's own auth check has already passed. Originally lived only in
routers/structured_reports.py (its download route); pulled out here once
verification.py needed the identical access to re-read a document's
source file for the independent second-model check -- same file, same
reasoning, just no longer duplicated.
"""
import os
from urllib.parse import urlparse

from supabase import create_client

STORAGE_BUCKET = "medical-documents"  # datafetch/clients.py's SupabaseClient.storage_bucket -- kept in sync by hand.

_client = None


def get_storage_client():
    """Lazy singleton. Never exposed to the frontend -- only used
    server-side after the caller's own ownership check already passed."""
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
        if not url or not key:
            raise RuntimeError("Supabase storage is not configured (SUPABASE_URL/SUPABASE_SERVICE_KEY)")
        _client = create_client(url, key)
    return _client


def storage_path_from_url(file_url: str) -> str:
    """documents.file_url is stored as a get_public_url()-shaped string
    (".../storage/v1/object/public/<bucket>/<path>") even though the
    bucket is actually private -- see get_storage_client's own doc
    comment. That URL 400s if fetched directly, but the <path> portion
    after the bucket name is exactly what .storage.from_(bucket).download()
    needs."""
    path = urlparse(file_url).path
    marker = f"/object/public/{STORAGE_BUCKET}/"
    idx = path.find(marker)
    if idx == -1:
        raise RuntimeError("Unrecognized file URL shape")
    return path[idx + len(marker):]


def download_file(file_url: str) -> bytes:
    return get_storage_client().storage.from_(STORAGE_BUCKET).download(storage_path_from_url(file_url))
