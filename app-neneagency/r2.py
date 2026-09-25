import os
import uuid
import boto3
from botocore.config import Config

# Load R2 config from environment
R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT          = os.getenv("R2_ENDPOINT")
R2_BUCKET            = os.getenv("R2_BUCKET")
R2_PUBLIC_URL        = os.getenv("R2_PUBLIC_URL")

# Create one S3 client to be reused
_s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)


def upload_file(local_path, content_type=None):
    """
    Upload a local file to R2 and return its public URL.
    Generates a random filename to avoid collisions and prevent guessing.
    """
    ext = os.path.splitext(local_path)[1].lower()  # keeps .jpg / .mp4 etc.
    key = f"{uuid.uuid4().hex}{ext}"

    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type

    _s3.upload_file(local_path, R2_BUCKET, key, ExtraArgs=extra_args)
    return f"{R2_PUBLIC_URL}/{key}", key


def delete_file(key):
    """Delete a file from R2 by its key (filename)."""
    _s3.delete_object(Bucket=R2_BUCKET, Key=key)