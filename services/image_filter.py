import cv2
import numpy as np

# 얼굴 인식을 위한 분류기 로드
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')

def analyze_image_v2(image_path):
    # 1. 이미지 로드
    image = cv2.imread(image_path)
    if image is None: 
        return "Error", "파일 읽기 실패"

    # 기본 정보 추출
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    
    # 2. 초점 체크 (Laplacian)
    laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    if laplacian_var < 80:
        return "Fail", f"초점 흐림({laplacian_var:.1f})"

    # 3. 밝기 체크
    avg_brightness = np.mean(gray)
    if avg_brightness < 40 or avg_brightness > 230:
        return "Fail", f"밝기 부적절({avg_brightness:.1f})"

    # 4. 바버샵 관련성 체크 (얼굴 인식 OR 피부색 비중)
    # 4-1. 얼굴 인식 시도
    faces = face_cascade.detectMultiScale(gray, 1.3, 5)
    
    # 4-2. 피부색 비중 계산 (얼굴이 안 보이는 뒷모습/옆모습 대비)
    # 이미지를 HSV 색공간으로 변환
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    
    # 일반적인 피부색 범위 (인종 및 조명 고려)
    # Lower: [H, S, V], Upper: [H, S, V]
    lower_skin = np.array([0, 20, 70], dtype=np.uint8)
    upper_skin = np.array([25, 255, 255], dtype=np.uint8)
    
    mask = cv2.inRange(hsv, lower_skin, upper_skin)
    skin_pixels = cv2.countNonZero(mask)
    skin_ratio = (skin_pixels / (height * width)) * 100

    # 판정 로직: 얼굴이 검출되었거나, 피부색 비중이 일정 수준(예: 5%) 이상이어야 함
    if len(faces) == 0 and skin_ratio < 5.0:
        # 얼굴도 없고 피부색도 거의 없다면 바버샵 홍보물(풍경, 음식, 빈 배경)이 아닐 확률이 높음
        return "Fail", f"관련성 낮음 (얼굴 미검출 및 피부 비중 {skin_ratio:.1f}%)"

    return "Pass", f"1차 통과 (피부비중: {skin_ratio:.1f}%)"

# 테스트용
# print(analyze_image_v2("test_image.jpg"))