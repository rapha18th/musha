"""Evidence the manager sends: kept once, with the bytes' own type, a sha256 and a perceptual hash.

The client's content type is a hint. The bytes decide: a photo must open as an image,
a voice note must look like audio, a quote may be a PDF. Photos are turned upright
from their EXIF orientation before hashing, so the stored bytes and the hashes agree.
"""
import hashlib
import io
import math

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Attachment, Shop, SpendRequest

KINDS = ("receipt", "goods", "quote", "voice", "photo")
MAX_BYTES = 8 * 1024 * 1024
SHOP_BYTES = 40 * 1024 * 1024  # all the evidence one shop keeps
MAX_SIDE = 2400  # longer photos are scaled down once, at upload
MAX_PIXELS = 40_000_000

# What each kind may carry
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
AUDIO_TYPES = ("audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/aac", "audio/amr")
ALLOWED = {
    "receipt": IMAGE_TYPES + ("application/pdf",),
    "goods": IMAGE_TYPES,
    "photo": IMAGE_TYPES,
    "quote": IMAGE_TYPES + ("application/pdf",),
    "voice": AUDIO_TYPES,
}


class EvidenceError(ValueError):
    pass


def sniff(data: bytes, hint: str = "") -> str | None:
    """The media type the bytes declare for themselves, or None."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"  # Matroska and WebM: what Chrome records
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"#!AMR"):
        return "audio/amr"
    if head[4:8] == b"ftyp":
        return "audio/mp4"  # Safari records voice as MP4 audio
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        # MPEG audio frames and ADTS AAC share a sync word; the hint tells them apart
        return "audio/aac" if "aac" in hint.lower() else "audio/mpeg"
    return None


def store_attachment(session: Session, shop: Shop, *, kind: str, data: bytes, content_type: str = "",
                     request_id: int | None = None) -> Attachment:
    """Check, normalise and keep one piece of evidence. Flushes; the caller commits."""
    if kind not in KINDS:
        raise EvidenceError(f"Evidence is one of {', '.join(KINDS)}.")
    req = session.get(SpendRequest, request_id) if request_id is not None else None
    if request_id is not None and (req is None or req.shop_id != shop.id):
        raise EvidenceError("That request belongs to another shop.")
    if not data:
        raise EvidenceError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise EvidenceError("Files up to 8 MB, please.")
    kept = session.scalar(select(func.coalesce(func.sum(Attachment.size), 0)).where(Attachment.shop_id == shop.id))
    if kept + len(data) > SHOP_BYTES:
        raise EvidenceError("This shop's evidence is full at 40 MB.")
    mime = sniff(data, content_type)
    if mime not in ALLOWED[kind]:
        raise EvidenceError(_refusal(kind))
    phash = ""
    if mime.startswith("image/"):
        data, mime = upright(data, mime)
        phash = perceptual_hash(data)
    a = Attachment(shop_id=shop.id, kind=kind, content_type=mime, data=data, size=len(data),
                   sha256=hashlib.sha256(data).hexdigest(), phash=phash)
    if req is not None:
        req.attachments.append(a)  # keeps the request's list current in this session
    session.add(a)
    session.flush()
    return a


def upright(data: bytes, mime: str) -> tuple[bytes, str]:
    """Apply EXIF orientation and cap the size. Bytes that need neither are kept exactly."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_PIXELS:
                raise EvidenceError("That photo is too large. Send a smaller one.")
            im.load()
            orientation = im.getexif().get(0x0112, 1)
            if orientation in (0, 1) and max(im.size) <= MAX_SIDE:
                return data, mime
            out = ImageOps.exif_transpose(im)
            if max(out.size) > MAX_SIDE:
                out.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            if out.mode not in ("RGB", "L"):
                out = out.convert("RGB")
            buf = io.BytesIO()
            out.save(buf, "JPEG", quality=88, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise EvidenceError("Send the photo again as a JPEG or PNG. This one is damaged or in another format.")


# Perceptual hashes. The 64-bit hash is kept on every photo and finds candidates fast: copies
# that were recompressed, resized, forwarded or touched up. The 256-bit hash looks at finer
# detail and confirms a candidate, so two photos of different receipts taken on the same
# counter from the same angle are not taken for one another.

PHASH_NEAR = 10  # bits of 64: a candidate copy
FINE_NEAR = 28  # bits of 256: a confirmed copy

_COS = {}


def _dct_bits(data: bytes, n: int, k: int) -> int | None:
    """The k x k lowest DCT frequencies of an n x n grey thumbnail, one bit each against their median."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.draft("L", (n * 4, n * 4))  # JPEG decodes small and fast
            grey = ImageOps.exif_transpose(im).convert("L").resize((n, n), Image.LANCZOS)
    except (UnidentifiedImageError, OSError):
        return None
    cos = _COS.get((n, k))
    if cos is None:
        cos = _COS[(n, k)] = [[math.cos((2 * x + 1) * u * math.pi / (2 * n)) for x in range(n)]
                              for u in range(k)]
    px = grey.tobytes()
    rows = [px[r * n:(r + 1) * n] for r in range(n)]
    partial = [[sum(c * v for c, v in zip(cos[u], row)) for u in range(k)] for row in rows]
    coeffs = [sum(cos[v][y] * partial[y][u] for y in range(n)) for v in range(k) for u in range(k)]
    rest = sorted(coeffs[1:])  # the DC term carries brightness alone
    median = rest[len(rest) // 2]
    bits = 0
    for c in coeffs:
        bits = (bits << 1) | (c > median)
    return bits


def perceptual_hash(data: bytes) -> str:
    """64-bit DCT hash of the photo, as 16 hex characters; empty when the bytes are no image."""
    bits = _dct_bits(data, 32, 8)
    return "" if bits is None else f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    """Bits that differ between two perceptual hashes; 64 when either is missing."""
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def same_photo(a: Attachment, b: Attachment) -> bool:
    """One photo, or a copy of it: identical bytes, or both hashes close."""
    if a.sha256 == b.sha256:
        return True
    if hamming(a.phash, b.phash) > PHASH_NEAR:
        return False
    fa, fb = _dct_bits(a.data, 64, 16), _dct_bits(b.data, 64, 16)
    return fa is not None and fb is not None and bin(fa ^ fb).count("1") <= FINE_NEAR


def _refusal(kind: str) -> str:
    if kind == "voice":
        return "A voice note must be an audio recording."
    if kind in ("receipt", "quote"):
        return "Send the receipt as a photo (JPEG or PNG) or a PDF."
    return "Send the photo as a JPEG or PNG."
