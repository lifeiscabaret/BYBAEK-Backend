"""
기능   : 선택된 사진의 "실제 이미지"를 Claude 멀티모달 입력(content block)으로 변환
배경   :
    - 예전엔 캡션 생성 시 사진 정보로 style_tags(텍스트 태그)만 프롬프트에 넣었다.
      그래서 어떤 사진이든 "페이드컷/스킨페이드" 같은 카테고리 수준의 일반론 캡션이 나왔다.
    - Claude Sonnet 4.6은 비전 입력을 지원하므로, 사진 픽셀 자체를 함께 보내면
      그 사진만의 디테일(표정/조명/그날 결과물 특징)을 캡션에 녹일 수 있다.
사용   :
    from utils.photo_vision import build_photo_image_blocks
    blocks = await build_photo_image_blocks(selected_photos)   # 실패 시 [] (텍스트만으로 폴백)
"""

import asyncio
import base64
import io

import httpx

from services.blob_storage import generate_sas_url

# 비용/지연 관리: 사진을 무제한으로 보내면 토큰과 응답 시간이 커진다.
# 대표 사진 위주로 최대 2장만 전송한다.
MAX_IMAGES = 2

# Anthropic 권장 상한. 이보다 커도 내부에서 축소되므로 토큰만 더 쓴다.
MAX_LONG_EDGE = 1568
JPEG_QUALITY = 85

DOWNLOAD_TIMEOUT = 10.0   # 사진 한 장당 다운로드 제한 (파이프라인이 멈추면 안 됨)
MAX_IMAGE_BYTES = 4_500_000   # API 이미지당 5MB 한도 대비 여유분


def _to_jpeg_bytes(raw: bytes) -> bytes:
    """원본 이미지 바이트 → 장변 MAX_LONG_EDGE 이하 JPEG 바이트.

    - HEIC/HEIF는 pillow-heif가 설치돼 있으면 함께 처리된다(업로드 파이프라인과 동일).
    - EXIF 회전 정보를 반영해서 세로 사진이 눕는 것을 막는다.
    - 항상 JPEG로 재인코딩하므로 media_type은 image/jpeg로 고정할 수 있다.
    """
    from PIL import Image, ImageOps

    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass   # HEIC 아닌 경우가 대부분 — 없어도 jpg/png는 그대로 열린다

    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    long_edge = max(img.size)
    if long_edge > MAX_LONG_EDGE:
        ratio = MAX_LONG_EDGE / long_edge
        img = img.resize(
            (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
            Image.LANCZOS,
        )

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


async def _load_one(blob_url: str) -> str | None:
    """blob_url 한 개 → base64 JPEG 문자열. 실패하면 None."""
    try:
        # SAS 생성은 blocking(연결문자열 파싱 + 서명) → 이벤트 루프 밖에서 실행
        sas_url = await asyncio.to_thread(generate_sas_url, blob_url, 1)

        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT) as client:
            resp = await client.get(sas_url)
            resp.raise_for_status()
            raw = resp.content

        jpeg = await asyncio.to_thread(_to_jpeg_bytes, raw)
        if len(jpeg) > MAX_IMAGE_BYTES:
            print(f"[photo_vision] 리사이즈 후에도 용량 초과({len(jpeg)}B) → 이 사진은 제외")
            return None

        return base64.b64encode(jpeg).decode("ascii")

    except Exception as e:
        print(f"[photo_vision] 이미지 로드 실패 ({blob_url}): {e}")
        return None


async def build_photo_image_blocks(selected_photos: list, max_images: int = MAX_IMAGES) -> list:
    """선택된 사진들 → Anthropic Messages API image content block 리스트.

    한 장이라도 실패하면 그 사진만 건너뛰고, 전부 실패하면 빈 리스트를 반환한다.
    호출부는 빈 리스트일 때 기존 동작(텍스트 태그만)으로 자연스럽게 폴백하면 된다.
    """
    if not selected_photos:
        return []

    urls = []
    for photo in selected_photos:
        url = (photo or {}).get("blob_url")
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= max_images:
            break

    if not urls:
        print("[photo_vision] blob_url 있는 사진 없음 → 이미지 전송 생략")
        return []

    encoded = await asyncio.gather(*(_load_one(u) for u in urls))

    blocks = []
    for data in encoded:
        if not data:
            continue
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": data,
            },
        })

    print(f"[photo_vision] 이미지 {len(blocks)}/{len(urls)}장 준비 완료")
    return blocks
