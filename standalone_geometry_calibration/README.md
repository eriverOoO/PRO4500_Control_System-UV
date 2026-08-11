# Standalone geometry calibration

이 앱은 기존 `structured_light_pc_controller.py`와 PCB 디코더를 수정하거나
호출하지 않고 카메라–프로젝터 기하 보정을 독립적으로 수행합니다.

## 최초 설정

`calibration_config.json`에서 실제 장비에 맞게 다음 값을 확인합니다.

- `checkerboard.inner_corners`: 체커보드 내부 코너의 열×행 개수
- `checkerboard.square_size_mm`: 여러 칸을 캘리퍼스로 측정한 실제 평균 한 칸 크기
- `projector.monitor_index`, `width_px`, `height_px`: Windows 프로젝터 화면
- `stage_aruco.marker_size_mm`, `marker_ids`: 스테이지에 부착한 마커 정보

제공된 A4 보정판은 12 mm 정사각형 22×15칸, 내부 코너 21×14 규격이며
기본 설정이 이 PDF와 일치합니다.

체커보드 외곽은 직사각형, 원형 어느 쪽이어도 무관합니다. 코너 격자와 한 칸의
실제 크기만 사용합니다.

## 사용

`run_standalone_geometry_calibration.bat`을 실행합니다.

1. `새 세션 + 패턴 준비`를 한 번 누릅니다. X/Y Gray와 4-step sine 패턴이 세션에 미리 저장됩니다.
2. 체커보드를 카메라와 프로젝터 모두에 보이게 놓고 `다음 pose 자동 촬영`을 누릅니다.
3. 한 pose가 끝날 때까지 체커보드를 고정합니다. 완료 후 위치·거리·기울기를 임의로 바꿉니다.
4. 10–16개 pose를 권장합니다. 이동량이나 각도는 입력하지 않습니다.
5. `기하 보정 계산`을 누르면 `geometry_calibration.json`이 생성됩니다.
6. 체커보드를 치우고 ArUco 스테이지가 보이게 한 뒤 `ArUco 스테이지 z=0 촬영`을 누릅니다.

각 pose는 projector-black, full-white, X/Y Gray 역상쌍과 X/Y 4-step sine를
자동 촬영합니다. 체커보드는 `full-white - black` 영상에서 검출하며, 검출 실패나
너무 작은 보드는 해당 pose를 거부합니다.

흑색 잉크 영역은 UV 패턴을 강하게 흡수할 수 있으므로, 프로그램은 체커보드 코너
한 점의 패턴 값을 직접 사용하지 않습니다. 각 코너 주변 47 px 패치에서 Gray 대비와
sine modulation이 살아 있는 픽셀만 골라 국소 카메라-프로젝터 homography를 맞추고
그 변환으로 코너의 프로젝터 좌표를 추정합니다.

ArUco 마커 종이/필름에 두께가 있으면 계산되는 `z=0`은 금속 스테이지 표면이 아니라
마커 인쇄면입니다. 금속면 기준이 필요하면 그 두께를 후속 계산에서 보정해야 합니다.
