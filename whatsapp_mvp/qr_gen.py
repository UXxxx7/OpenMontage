# WhatsApp MVP - QR code generation for the contact/CTA close.
#
# Ported from video-studio's motion/retirement-fund-fresh/build_data.py — the
# more mature of two QR-generation patterns found there (the other,
# vell-renewal-reminder's, unconditionally generates a QR even for mock data).
# This one follows the project's own "Facts doctrine": never fabricate a
# contact QR code for a job that didn't actually supply one. Only called from
# _op_apply_style when op["qr_contact"] carries a real contact URL.

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def generate_qr(contact_url: Optional[str], output_path: Path) -> bool:
    """Generate a QR code PNG for `contact_url` at `output_path`.

    Returns False (and writes nothing) if `contact_url` is falsy — this
    function never invents a placeholder QR code for a contact that wasn't
    actually supplied.
    """
    if not contact_url:
        logger.info("qr_gen: 没有真实联系方式，跳过 QR 生成（不编造占位符）")
        return False

    import qrcode

    qr = qrcode.QRCode(border=2, box_size=20, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(contact_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1F1B16", back_color="white")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path))
    logger.info(f"qr_gen: 生成 QR -> {output_path}")
    return True
