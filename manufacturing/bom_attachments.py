from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.utils.text import get_valid_filename


ALLOWED_BOM_ATTACHMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
ALLOWED_BOM_ATTACHMENT_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "application/pdf",
}


def validate_bom_attachment(file_name, content_type=None):
    name = str(file_name or "").strip()
    ext = Path(name).suffix.lower()
    mime_type = str(content_type or "").split(";")[0].strip().lower()

    if ext not in ALLOWED_BOM_ATTACHMENT_EXTENSIONS:
        raise ValueError("BOM attachment must be a JPG, PNG, or PDF file.")
    if mime_type and mime_type not in ALLOWED_BOM_ATTACHMENT_MIME_TYPES:
        raise ValueError("BOM attachment must be a JPG, PNG, or PDF file.")


def _safe_attachment_name(file_name, content_type=None):
    fallback_ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "application/pdf": ".pdf",
    }.get(str(content_type or "").split(";")[0].strip().lower(), "")
    raw_name = str(file_name or "").strip() or f"bom-attachment{fallback_ext}"
    safe_name = get_valid_filename(Path(raw_name).name)
    if not Path(safe_name).suffix and fallback_ext:
        safe_name = f"{safe_name}{fallback_ext}"
    return safe_name or f"bom-attachment{fallback_ext or '.pdf'}"


def save_bom_attachment(bom, file_obj, *, file_name=None, content_type=None):
    if not file_obj:
        return

    if isinstance(file_obj, UploadedFile):
        file_name = file_name or file_obj.name
        content_type = content_type or getattr(file_obj, "content_type", "")
        validate_bom_attachment(file_name, content_type)
        safe_name = _safe_attachment_name(file_name, content_type)
        bom.attachment.save(safe_name, file_obj, save=False)
    else:
        validate_bom_attachment(file_name, content_type)
        safe_name = _safe_attachment_name(file_name, content_type)
        bom.attachment.save(safe_name, ContentFile(file_obj), save=False)

    bom.attachment_name = str(file_name or safe_name)
    bom.save(update_fields=["attachment", "attachment_name"])


def serialize_bom_attachment(bom, request=None):
    if not bom or not getattr(bom, "attachment", None):
        return None

    try:
        url = bom.attachment.url
    except ValueError:
        return None

    name = getattr(bom, "attachment_name", "") or Path(bom.attachment.name).name
    ext = Path(name or bom.attachment.name).suffix.lower()
    payload = {
        "name": name,
        "url": request.build_absolute_uri(url) if request else url,
        "download_url": request.build_absolute_uri(url) if request else url,
        "is_image": ext in {".jpg", ".jpeg", ".png"},
        "is_pdf": ext == ".pdf",
    }
    return payload
