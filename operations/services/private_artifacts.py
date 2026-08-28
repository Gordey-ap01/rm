"""Private, write-once local storage for sensitive application artifacts."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 2_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 100
STORAGE_PREFIX = "donor-report-submissions"
STORAGE_KEY_RE = re.compile(
    r"^donor-report-submissions/snapshot-(?P<snapshot>[1-9][0-9]*)/"
    r"submission-(?P<number>[0-9]{6})/(?P<sha256>[0-9a-f]{64})"
    r"\.(?P<extension>pdf|docx|xlsx|odt|ods)$"
)

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ODT_MIME = "application/vnd.oasis.opendocument.text"
ODS_MIME = "application/vnd.oasis.opendocument.spreadsheet"
MIME_TO_EXTENSION = {
    PDF_MIME: "pdf",
    DOCX_MIME: "docx",
    XLSX_MIME: "xlsx",
    ODT_MIME: "odt",
    ODS_MIME: "ods",
}


class ArtifactIntegrityError(Exception):
    """Stored bytes do not match their immutable database metadata."""


@dataclass(frozen=True)
class StagedArtifact:
    path: Path
    original_filename: str
    content_type: str
    extension: str
    file_size: int
    file_sha256: str


def private_artifact_root() -> Path:
    root = Path(settings.PRIVATE_ARTIFACT_ROOT).resolve()
    media_root = Path(settings.MEDIA_ROOT).resolve()
    static_root = Path(settings.STATIC_ROOT).resolve()
    if root == media_root or media_root in root.parents or root in media_root.parents:
        raise ImproperlyConfigured("PRIVATE_ARTIFACT_ROOT overlaps MEDIA_ROOT.")
    if root == static_root or static_root in root.parents or root in static_root.parents:
        raise ImproperlyConfigured("PRIVATE_ARTIFACT_ROOT overlaps STATIC_ROOT.")
    _ensure_directory_chain(root)
    _restrict_permissions(root, directory=True)
    return root


def _restrict_permissions(path: Path, *, directory: bool = False) -> None:
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        if os.name != "nt":
            raise


def _safe_original_filename(raw_name: object) -> str:
    name = unicodedata.normalize("NFKC", str(raw_name or ""))
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        character
        for character in name
        if unicodedata.category(character) not in {"Cc", "Cf"}
    )
    name = name.strip().strip(".")
    if not name:
        raise ValidationError({"file": "У файла отсутствует допустимое имя."})
    if len(name) > 255:
        stem = Path(name).stem[:220].rstrip()
        suffix = Path(name).suffix[:15]
        name = f"{stem}{suffix}"
    return name


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    posix_name = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or ".." in posix_name.parts
        or (posix_name.parts and ":" in posix_name.parts[0])
    ):
        raise ValidationError({"file": "Архив содержит небезопасный путь."})
    if info.flag_bits & 0x1:
        raise ValidationError({"file": "Зашифрованные документы не поддерживаются."})
    file_mode = (info.external_attr >> 16) & 0o170000
    if file_mode not in {0, 0o040000, 0o100000}:
        raise ValidationError({"file": "Архив содержит ссылку или специальный файл."})
    if (
        info.file_size > 1024 * 1024
        and info.compress_size > 0
        and info.file_size / info.compress_size > MAX_ARCHIVE_COMPRESSION_RATIO
    ):
        raise ValidationError({"file": "Коэффициент сжатия документа небезопасен."})


def _zip_content_type(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValidationError({"file": "В документе слишком много вложенных частей."})
            total_size = 0
            names = set()
            lower_names = set()
            for member in members:
                _validate_zip_member(member)
                total_size += member.file_size
                if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise ValidationError(
                        {"file": "Распакованный размер документа превышает безопасный предел."}
                    )
                lowered_name = member.filename.casefold()
                if member.filename in names or lowered_name in lower_names:
                    raise ValidationError(
                        {"file": "Архив содержит повторяющиеся имена частей."}
                    )
                names.add(member.filename)
                lower_names.add(lowered_name)
            if any(name.endswith("vbaproject.bin") for name in lower_names):
                raise ValidationError({"file": "Документы с макросами запрещены."})
            if any(
                name.startswith(("scripts/", "basic/", "meta-inf/scripts/"))
                for name in lower_names
            ):
                raise ValidationError({"file": "Документы со встроенными скриптами запрещены."})
            broken_member = archive.testzip()
            if broken_member is not None:
                raise ValidationError({"file": "ZIP-структура документа повреждена."})

            if "mimetype" in names:
                mimetype = archive.read("mimetype").decode("ascii", errors="strict").strip()
                if mimetype in {ODT_MIME, ODS_MIME}:
                    return mimetype

            if "[Content_Types].xml" not in names:
                raise ValidationError({"file": "Не удалось определить формат документа."})
            content_types_bytes = archive.read("[Content_Types].xml")
            if len(content_types_bytes) > 5 * 1024 * 1024:
                raise ValidationError({"file": "Служебная структура документа слишком велика."})
            if b"<!DOCTYPE" in content_types_bytes.upper() or b"<!ENTITY" in content_types_bytes.upper():
                raise ValidationError({"file": "XML-описание документа содержит запрещенные сущности."})
            try:
                root = ElementTree.fromstring(content_types_bytes)
            except ElementTree.ParseError as exc:
                raise ValidationError({"file": "Служебная структура документа повреждена."}) from exc
            declared_types = {
                element.attrib.get("ContentType", "")
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] in {"Default", "Override"}
            }
            if any("macroenabled" in value.lower() for value in declared_types):
                raise ValidationError({"file": "Документы с макросами запрещены."})

            has_word = any(name.startswith("word/") for name in names)
            has_excel = any(name.startswith("xl/") for name in names)
            if has_word and not has_excel and any(
                value.endswith("wordprocessingml.document.main+xml")
                for value in declared_types
            ):
                return DOCX_MIME
            if has_excel and not has_word and any(
                value.endswith("spreadsheetml.sheet.main+xml") for value in declared_types
            ):
                return XLSX_MIME
    except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValidationError({"file": "Не удалось безопасно прочитать документ."}) from exc
    raise ValidationError({"file": "Поддерживаются PDF, DOCX, XLSX, ODT и ODS."})


def detect_content_type(path: Path) -> str:
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if prefix.startswith(b"%PDF-"):
            stream.seek(max(path.stat().st_size - 4096, 0))
            if b"%%EOF" not in stream.read():
                raise ValidationError({"file": "PDF не содержит корректного завершения."})
            return PDF_MIME
    if zipfile.is_zipfile(path):
        return _zip_content_type(path)
    raise ValidationError({"file": "Поддерживаются PDF, DOCX, XLSX, ODT и ODS."})


def stage_upload(uploaded_file) -> StagedArtifact:
    root = private_artifact_root()
    staging_root = root / ".staging"
    _ensure_directory_chain(staging_root)
    _restrict_permissions(staging_root, directory=True)
    descriptor, raw_path = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=staging_root)
    path = Path(raw_path)
    os.close(descriptor)
    _restrict_permissions(path)
    _fsync_directory(staging_root)
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("wb") as target:
            for chunk in uploaded_file.chunks():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValidationError({"file": "Файл превышает ограничение 25 MiB."})
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if size == 0:
            raise ValidationError({"file": "Пустой файл нельзя сохранить."})
        content_type = detect_content_type(path)
        extension = MIME_TO_EXTENSION[content_type]
        original_filename = _safe_original_filename(getattr(uploaded_file, "name", ""))
        if Path(original_filename).suffix.lower() != f".{extension}":
            raise ValidationError(
                {"file": f"Содержимое файла не соответствует расширению .{extension}."}
            )
        return StagedArtifact(
            path=path,
            original_filename=original_filename,
            content_type=content_type,
            extension=extension,
            file_size=size,
            file_sha256=digest.hexdigest(),
        )
    except Exception:
        _unlink_and_fsync(path)
        raise


def build_storage_key(
    *,
    snapshot_id: int,
    submission_number: int,
    file_sha256: str,
    extension: str,
) -> str:
    if snapshot_id < 1 or submission_number < 1:
        raise ValueError("snapshot_id and submission_number must be positive.")
    if not re.fullmatch(r"[0-9a-f]{64}", file_sha256):
        raise ValueError("file_sha256 must be lowercase SHA-256.")
    if extension not in MIME_TO_EXTENSION.values():
        raise ValueError("Unsupported private artifact extension.")
    return (
        f"{STORAGE_PREFIX}/snapshot-{snapshot_id}/"
        f"submission-{submission_number:06d}/{file_sha256}.{extension}"
    )


def validate_storage_key(storage_key: str) -> re.Match[str]:
    match = STORAGE_KEY_RE.fullmatch(storage_key)
    if not match:
        raise ArtifactIntegrityError("Private artifact storage key is invalid.")
    return match


def resolve_storage_key(storage_key: str) -> Path:
    validate_storage_key(storage_key)
    root = private_artifact_root()
    path = (root / PurePosixPath(storage_key)).resolve()
    if root not in path.parents:
        raise ArtifactIntegrityError("Private artifact path escapes its storage root.")
    current = root
    for part in PurePosixPath(storage_key).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ArtifactIntegrityError("Private artifact path contains a symbolic link.")
    return path


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory_chain(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current == current.parent:
            break
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        _restrict_permissions(directory, directory=True)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)


def _unlink_and_fsync(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def publish_staged_artifact(
    staged: StagedArtifact,
    storage_key: str,
    *,
    allow_verified_existing: bool = False,
) -> tuple[Path, bool]:
    path = resolve_storage_key(storage_key)
    _ensure_directory_chain(path.parent)
    _restrict_permissions(path.parent, directory=True)
    owns_final_object = False
    try:
        try:
            os.link(staged.path, path)
            owns_final_object = True
        except FileExistsError as exc:
            if not allow_verified_existing:
                raise ValidationError(
                    {
                        "file": (
                            "Этот неизменяемый storage key уже занят. "
                            "Запустите integrity scan."
                        )
                    }
                ) from exc
            read_verified_artifact(
                storage_key=storage_key,
                expected_size=staged.file_size,
                expected_sha256=staged.file_sha256,
                expected_content_type=staged.content_type,
            )
            _restrict_permissions(path)
            _unlink_and_fsync(staged.path)
            return path, False
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            try:
                with staged.path.open("rb") as source, path.open("xb") as target:
                    owns_final_object = True
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            except FileExistsError as exists_exc:
                if not allow_verified_existing:
                    raise ValidationError(
                        {
                            "file": (
                                "Этот неизменяемый storage key уже занят. "
                                "Запустите integrity scan."
                            )
                        }
                    ) from exists_exc
                read_verified_artifact(
                    storage_key=storage_key,
                    expected_size=staged.file_size,
                    expected_sha256=staged.file_sha256,
                    expected_content_type=staged.content_type,
                )
                _restrict_permissions(path)
                _unlink_and_fsync(staged.path)
                return path, False
        _restrict_permissions(path)
        _fsync_directory(path.parent)
        _unlink_and_fsync(staged.path)
        return path, True
    except Exception:
        if owns_final_object:
            try:
                _unlink_and_fsync(path)
            except OSError as cleanup_exc:
                raise ArtifactIntegrityError(
                    "Не удалось удалить незавершенный private artifact; "
                    "требуется integrity scan."
                ) from cleanup_exc
        raise


def remove_published_artifact(storage_key: str) -> None:
    _unlink_and_fsync(resolve_storage_key(storage_key))


def discard_staged_artifact(staged: StagedArtifact | None) -> None:
    if staged is not None:
        _unlink_and_fsync(staged.path)


def read_verified_artifact(
    *,
    storage_key: str,
    expected_size: int,
    expected_sha256: str,
    expected_content_type: str,
) -> bytes:
    path = resolve_storage_key(storage_key)
    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise ArtifactIntegrityError("Private artifact is missing.") from exc
    if not path.is_file() or stat.st_size != expected_size:
        raise ArtifactIntegrityError("Private artifact size does not match metadata.")
    digest = hashlib.sha256()
    payload = bytearray()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            payload.extend(chunk)
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ArtifactIntegrityError("Private artifact SHA-256 does not match metadata.")
    try:
        detected_content_type = detect_content_type(path)
    except ValidationError as exc:
        raise ArtifactIntegrityError("Private artifact content is no longer valid.") from exc
    if detected_content_type != expected_content_type:
        raise ArtifactIntegrityError("Private artifact MIME does not match metadata.")
    return bytes(payload)


def iter_final_storage_keys() -> set[str]:
    root = private_artifact_root()
    prefix = root / STORAGE_PREFIX
    if not prefix.exists():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in prefix.rglob("*")
        if path.is_file()
    }


def iter_staging_paths() -> set[Path]:
    root = private_artifact_root()
    staging = root / ".staging"
    if not staging.exists():
        return set()
    return {path for path in staging.rglob("*") if path.is_file()}
