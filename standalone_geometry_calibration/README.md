# Standalone geometry calibration

## Exposure profiles

The app uses separate fixed exposures for ChArUco detection and structured-light
decoding. `checkerboard.detection_exposure_us` is used only with the projector
black frame to locate ChArUco IDs and corners. `camera.ximea.exposure_us` is
restored before the Black/White, Gray-code, and sine sequences are captured.

The current defaults are 2,000,000 us (2000 ms) for ChArUco detection and
200,000 us (200 ms) for structured-light patterns, both at 0 dB gain. Long
exposure detection images are saved separately and are never used as the
structured-light black reference.

이 앱은 기존 `structured_light_pc_controller.py`와 PCB 디코더를 수정하거나
호출하지 않고 카메라–프로젝터 기하 보정을 독립적으로 수행합니다.

## 최초 설정

`calibration_config.json`에서 실제 장비에 맞게 다음 값을 확인합니다.

- 보정 타깃은 `DICT_5X5_250` ChArUco이며, 일부만 보여도 코너 ID로 전체 보드 좌표를 복원합니다.
- `checkerboard.square_size_mm`: 인쇄된 ChArUco 한 칸의 실제 크기
- `checkerboard.marker_size_mm`: ArUco 마커 검은 외곽의 실제 크기
- UV 대비 문제를 줄이기 위해 프로젝터 OFF 영상과 CLAHE 영상을 우선 검출하고, 구조광 좌표는 코너 주변의 유효한 백색 영역에서 국소 호모그래피로 추정합니다.
- `projector.monitor_index`, `width_px`, `height_px`: Windows 프로젝터 화면
- `stage_aruco.marker_size_mm`, `marker_ids`: 스테이지에 부착한 별도 마커 정보. 크기는 흰색 사각형 전체가 아니라 ArUco의 검은 외곽 사각형 한 변을 캘리퍼스로 잰 값입니다. 현재 스테이지 마커 실측값은 `12 mm`이므로 기본값은 `12.0`입니다. ChArUco 보정판 내부 마커의 `9 mm`와 혼동하면 안 됩니다.

제공된 A4 보정판은 12 mm 정사각형 22×15칸, 내부 코너 21×14 규격이며
기본 설정이 이 PDF와 일치합니다.

체커보드 외곽은 직사각형, 원형 어느 쪽이어도 무관합니다. 코너 격자와 한 칸의
실제 크기만 사용합니다. 전체 A4 보드가 한 화면에 들어올 필요도 없습니다. 각 pose에서
보이는 연속 내부 코너 블록에서 가로·세로 각각 4~7인 모든 조합을 검사합니다.
예를 들어 4×4, 4×5, 5×4, 6×7, 7×6, 7×7이 모두 지원되며, 검출 가능한
가장 큰 격자를 해당 pose의 임시 로컬 좌표계로 사용합니다.

## 사용

`run_standalone_geometry_calibration.bat`을 실행합니다.

1. `새 세션 + 패턴 준비`를 한 번 누릅니다. X/Y Gray와 4-step sine 패턴이 세션에 미리 저장됩니다.
2. 체커보드를 카메라와 프로젝터 모두에 보이게 놓고 `다음 pose 자동 촬영`을 누릅니다.
3. 한 pose가 끝날 때까지 체커보드를 고정합니다. 완료 후 위치·거리·기울기를 임의로 바꿉니다.
4. 10–16개 pose를 권장합니다. 이동량이나 각도는 입력하지 않습니다.
5. `기하 보정 계산`을 누르면 `geometry_calibration.json`이 생성됩니다.
6. 체커보드를 치우고 ArUco 스테이지가 보이게 한 뒤 `ArUco 스테이지 z=0 촬영`을 누릅니다.

`stage_aruco.marker_size_mm`를 바꾸면 기존 `geometry_calibration.json`의
`stage_plane`은 이전 크기 기준이므로 그대로 사용하면 안 됩니다. 카메라-프로젝터
기하 보정 pose를 다시 촬영할 필요는 없고, 6번의 ArUco stage z=0 등록만 다시
실행하면 새 마커 크기로 `stage_plane`이 갱신됩니다.

ArUco z=0 촬영은 카메라 좌표계의 물리적 평면을 정합니다. 이후 실제 메인 패턴과
ChArUco stereo 기하의 projector 좌표가 일치하는지도 별도로 검증해야 합니다.
빈 평면을 메인 스캔과 동일한 22개 패턴으로 한 번 촬영한 다음 조직 디코더 저장소의
`scripts/register_geometric_stage_scan.py`를 실행하세요. 스크립트는 12 mm stage
마커로 얻은 평면과 구조광 projector-Y를 비교해 런타임
`geometry_calibration.json`에 좌표 보정을 기록합니다. 물체가 놓인 스캔은 z=0
등록에 사용하면 안 됩니다.

각 pose는 projector-black, full-white, X/Y Gray 역상쌍과 X/Y 4-step sine를
자동 촬영합니다. 체커보드는 `full-white - black` 영상에서 검출하며, 검출 실패나
너무 작은 보드는 해당 pose를 거부합니다.

흑색 잉크 영역은 UV 패턴을 강하게 흡수할 수 있으므로, 프로그램은 체커보드 코너
한 점의 패턴 값을 직접 사용하지 않습니다. 각 코너 주변 47 px 패치에서 Gray 대비와
sine modulation이 살아 있는 픽셀만 골라 국소 카메라-프로젝터 homography를 맞추고
그 변환으로 코너의 프로젝터 좌표를 추정합니다.

ArUco 마커 종이/필름에 두께가 있으면 계산되는 `z=0`은 금속 스테이지 표면이 아니라
마커 인쇄면입니다. 금속면 기준이 필요하면 그 두께를 후속 계산에서 보정해야 합니다.
