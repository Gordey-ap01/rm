"""Early request-size guards for sensitive uploads."""

from __future__ import annotations

from django.core.files.uploadhandler import FileUploadHandler, StopUpload

from operations.services.private_artifacts import MAX_UPLOAD_BYTES


class PrivateArtifactSizeLimitUploadHandler(FileUploadHandler):
    """Abort a multipart upload as soon as one file exceeds 25 MiB."""

    def new_file(self, *args, **kwargs) -> None:
        super().new_file(*args, **kwargs)
        self._received_bytes = 0

    def receive_data_chunk(self, raw_data: bytes, start: int) -> bytes:
        self._received_bytes += len(raw_data)
        if self._received_bytes > MAX_UPLOAD_BYTES:
            raise StopUpload(connection_reset=False)
        return raw_data

    def file_complete(self, file_size: int):
        return None
