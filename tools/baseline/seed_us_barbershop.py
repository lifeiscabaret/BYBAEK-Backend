"""u.s.barbershop (용산 삼각지) 실제 게시글 8건 — 2단계 콜드스타트 진단용 시드.

출처: 실제 독립 바버샵 인스타 계정. 사람이 직접 쓴 원본 (AI 생성 아님).
DB에 쓰지 않고 실험 스크립트가 import해서 쓴다.
"""

# 8개 게시글 전부에 붙어 있는 하단 고정 해시태그
FIXED_TAGS = ("#삼각지바버샵 #용산바버샵 #이태원바버샵 #신용산바버샵 #us바버샵용산본점 "
              "#us바버샵 #한강로바버샵 #공덕바버샵 #삼각지역바버샵 #이태원헤어샵추천 "
              "#용산헤어샵추천 #바버샵 #외국인바버샵")

# 8개 게시글 전부에 붙어 있는 예약/영업정보 블록
INFO_BLOCK = """✂ US바버샵 용산본점💥
▷ Since 2002, 용산 미군부대 영내 바버샵 경력
▷ 아메리칸 페이드컷 20년이상 경력 정통 바버샵
▷ 펌 시술❌(다운펌,아이롱펌,염색등 하지 않습니다)
▷ 모든스타일 컷,드라이,스타일링으로 완성

유명 배우및 유명셀럽, 해외 배우 등 헤어 전담 경력

Open(월-목)10:00-19:00, Open(금토일)09:00-19:00
∴예약: 네이버 검색창 "us바버샵용산본점"
∴문의: 02-790-7111
∴찾아오는 길: 서울 용산구 한강대로177 1층, 삼각지역(4호선)5번출구 도보20초, \
삼각지역(6호선)7번출구 도보1분, 주차:용산베르디움프렌즈 20분1000원 도보3분"""


def _post(intro, top_tags, likes=None, comments=0, note=""):
    body = f"{intro}\n\n{INFO_BLOCK}" if intro else INFO_BLOCK
    return {
        "intro": intro,
        "caption": body,
        "hashtags": f"{top_tags} {FIXED_TAGS}".split(),
        "likes": likes,
        "comments": comments,
        "note": note,
    }


POSTS = [
    _post("", "#freshhaircut #before #after #usbarbershop #seoulbarbershop",
          likes=58, note="2/24, 인트로 없음"),
    _post("찾아주신 고객님, 만족스러움에 더 감사드려요🙏",
          "#freshhaircut #sidepart #skinfade #us바버샵용산본점",
          likes=96, note="2/23, 인트로 있음 — 좋아요 최다"),
    _post("", "#freshhaircut #usbarbershop #seoulbarbershop",
          likes=48, note="1/10, 인트로 없음"),
    _post("", "#freshhaircut #before #after",
          likes=None, note="2025/6/13, 인트로 없음"),
    _post("5월 마지막날 #단체샷 #기념샷", "#단체샷 #기념샷",
          likes=88, note="팀 단체샷"),
    _post("", "#freshhaircut", likes=86, note="2025/4/27, 영상/무음, 인트로 없음"),
    _post("나이는 숫자에 불과! 너무 멋지십니다👍 #사이드파트 #포마드컷\n"
          "믿고 찾아주시는 고객님~ 더욱 멋지게 해드리겠습니다🥰\n"
          "내과 전문의 김상우 원장님! 다수 방송 출현으로 우리몸과 건강에 대한 유익한 지식과 "
          "정보를 알려주시는데요- 원장님의 스타일이 너무 멋지세요👍 피드 흔쾌히 허락해주셔서 "
          "너무 감사드립니다🙏",
          "#포마드컷 #사이드파트",
          likes=None, comments=5, note="2025/4/22, 댓글 5개 — 계정 내 참여 최고"),
    _post("", "", likes=None, note="팀 회식/음료 사진, 소셜 콘텐츠"),
]

# 이 계정의 실제 운영 정보에서 그대로 끌어온 브랜드 설정
BRAND_SETTINGS = {
    "brand_tone": ["클래식 프리미엄"],
    "forbidden_words": ["저렴", "할인"],
    "preferred_styles": ["아메리칸 페이드컷", "사이드파트", "포마드컷", "스킨페이드"],
    "exclude_conditions": ["펌", "다운펌", "아이롱펌", "염색"],
    "hashtag_style": [FIXED_TAGS],
    "cta": '예약: 네이버 검색창 "us바버샵용산본점" / 문의: 02-790-7111',
    "shop_intro": ("Since 2002, 용산 미군부대 영내 바버샵 경력. "
                   "아메리칸 페이드컷 20년이상 경력 정통 바버샵. "
                   "펌 시술 하지 않고 모든 스타일을 컷·드라이·스타일링으로 완성. "
                   "유명 배우 및 셀럽, 해외 배우 헤어 전담 경력."),
    "brand_differentiation": ("Since 2002, 용산 미군부대 영내 바버샵 경력. "
                              "아메리칸 페이드컷 20년이상 경력 정통 바버샵."),
    "target_customer_text": "용산·삼각지 인근 직장인과 주한 외국인, 정통 바버샵 컷을 찾는 남성",
    "feed_style": {"emoji_usage": "가끔", "caption_length": "2~4줄", "hashtag_count": 13},
    "language": "ko",
    "photo_range": {"min": 1, "max": 5},
}
