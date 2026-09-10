# Codex 인수인계: 2 mm / 6 mm 선형 보정 검증

이 문서는 이 저장소를 받은 다른 PC의 Codex가 별도 파일 위치 설명 없이 2 mm와 6 mm 블록 스캔을 검증하기 위한 실행 지침이다.

## 목표

- 저장소에 포함된 기존 선형 보정을 사용한다.
- 새로 받은 2 mm와 6 mm 촬영 폴더 또는 ZIP을 독립 검증 데이터로 사용한다.
- 2 mm와 6 mm 데이터로 보정계수를 다시 적합하지 않는다.
- 원본 촬영 데이터와 ZIP은 수정하지 않는다.

## 저장소 안의 보정 자료

항상 저장소 루트를 기준으로 다음 경로를 먼저 확인한다.

- 전달용 번들: `calibration_output/phase_linear_20260909_bundle.zip`
- 보정 파일: `calibration_output/phase_linear_20260909/phase_linear_20260909_2p95_5p03_7p96mm.npz`
- 0도 기준 위상: `calibration_output/phase_linear_20260909/flat_stage_reference/reference_phase_0.npy`
- 180도 기준 위상: `calibration_output/phase_linear_20260909/flat_stage_reference/reference_phase_180.npy`
- 상세 보고서: `calibration_output/phase_linear_20260909/phase_linear_calibration_report.json`
- 사용 안내: `calibration_output/phase_linear_20260909/README_KO.md`
- 무결성 값: `calibration_output/phase_linear_20260909/SHA256SUMS.json`

번들을 따로 찾거나 내려받지 않는다. 위 파일들은 Git 저장소에 포함되어 있다. 먼저 `SHA256SUMS.json`과 실제 파일 해시를 비교한다.

## Decoder 찾기

이 저장소는 촬영 제어 저장소이며 Python Decoder는 다른 로컬 저장소 또는 설치된 실행 파일에 있을 수 있다.

1. 현재 PC에서 `pcb_fpp_decoder`, `PCB_FPP_Decoder.exe`, `run_decoder.bat`을 검색한다.
2. 발견한 Decoder의 `AGENTS.md`, README 및 `--help`를 확인한다.
3. `phase_linear`, 방향별 reference phase, `swapped` 위상 규약을 지원하는 버전인지 확인한다.
4. Decoder가 전혀 없다면 임의의 비공식 프로그램을 만들거나 내려받지 말고, Decoder 설치 또는 저장소 위치가 필요하다고 사용자에게 보고한다.

## 고정 Decoder 설정

- height mode: `phase_linear`
- calibration config: 저장소 안의 `phase_linear_20260909_2p95_5p03_7p96mm.npz`
- reference phase 0: 저장소 안의 `reference_phase_0.npy`
- reference phase 180: 저장소 안의 `reference_phase_180.npy`
- phase convention: `swapped`
- phase axis: `auto` 또는 `y`
- phase direction: `normal`
- half-period correction: 활성화
- median filter: `3`
- min signal: `12`
- modulation threshold: `0.04`
- Gray pair min contrast: `0.04`
- fusion mode: `modulation-weighted`
- fusion registration: `aruco`
- ArUco dictionary: `DICT_4X4_50`
- ArUco IDs: `0,1,2,3`

보정계수는 물리적 0도 `5.882308 rad/mm`, 물리적 180도 `5.836248 rad/mm`, 높이 부호 `-1`이다.

GUI만 사용할 경우 `flat_stage_reference` 폴더를 `%LOCALAPPDATA%\PCB_FPP_Decoder\flat_stage_reference`에 복사할 수 있다. 기존 기준면이 있으면 먼저 백업하고 덮어쓴다.

## 물리 방향을 반드시 판정할 것

폴더 이름만 보고 `angle_000`을 물리적 0도로 가정하지 않는다. 각 스캔의 `aruco_validation/angle_000/validation_report.json` 또는 `accepted.png`를 읽는다.

물리적 0도에서 카메라 영상의 마커 배치는 다음과 같다.

- ID 0: 오른쪽
- ID 1: 아래쪽
- ID 2: 왼쪽
- ID 3: 위쪽

따라서 간단한 판정은 다음과 같다.

- ID 0의 중심 x가 ID 2보다 크면 해당 영상은 물리적 0도다.
- ID 0의 중심 x가 ID 2보다 작으면 해당 영상은 물리적 180도다.

스캔 시작 방향이 반대라면 입력 폴더를 물리 방향 기준으로 교환한다.

- 정상 시작: 0도 입력=`angle_000`, 180도 입력=`angle_180`
- 반대 시작: 0도 입력=`angle_180`, 180도 입력=`angle_000`

입력을 교환한 경우 촬영 폴더의 기존 `stage_precalibration.json`은 변환 방향이 반대이므로 사용하지 않는다. 물리 0도 폴더를 첫 입력, 물리 180도 폴더를 두 번째 입력으로 명시하고 ArUco 정합을 새로 계산한다. 이 경우 scan root 자동 선택이나 `--auto-phone-fusion`보다 두 방향 폴더를 명시적으로 전달하는 방식을 우선한다.

## 권장 CLI 형태

실제 옵션 이름은 설치된 Decoder의 `--help`로 확인하되, 지원되는 버전에서는 다음 형태를 사용한다.

```powershell
python -m pcb_fpp_decoder.cli `
  --input "<물리적 0도 angle 폴더>" `
  --input-180 "<물리적 180도 angle 폴더>" `
  --output "<검증 출력 폴더>" `
  --height-mode phase_linear `
  --reference-phase-0 "<저장소>\calibration_output\phase_linear_20260909\flat_stage_reference\reference_phase_0.npy" `
  --reference-phase-180 "<저장소>\calibration_output\phase_linear_20260909\flat_stage_reference\reference_phase_180.npy" `
  --calibration-config "<저장소>\calibration_output\phase_linear_20260909\phase_linear_20260909_2p95_5p03_7p96mm.npz" `
  --phase-convention swapped `
  --apply-half-period-correction `
  --median-filter 3 `
  --min-signal 12 `
  --modulation-threshold 0.04 `
  --gray-pair-min-contrast 0.04 `
  --fusion-registration aruco `
  --output-profile compact
```

## 높이 평가

전체 프레임 통계는 대부분 0 mm 스테이지를 나타내므로 블록 높이로 사용하지 않는다.

1. White 영상에서 중앙 블록 윗면을 검출한다.
2. 그림자, 외곽 모서리, 블록 측면, ArUco 마커와 낮은 변조 픽셀을 제외한다.
3. 필요하면 블록 마스크를 안쪽으로 침식해 경계 혼합을 제거한다.
4. 물리 0도, 물리 180도 및 융합 결과 각각에서 블록면 높이를 계산한다.
5. 다음을 보고한다.
   - 블록면 중앙값, 평균, 표준편차, p5/p95
   - 유효 픽셀 수와 비율
   - 물리 0도와 180도 중앙값 차이
   - 융합 중앙값
   - 캘리퍼 실측값 대비 signed/absolute error
   - ArUco 정합 RMSE 및 회전 오차
   - cycle-slip과 융합 거부 비율

캘리퍼 실측값이 제공되면 명목상 2.00/6.00 mm 대신 실측값을 사용한다. 실측값이 없으면 명목값으로 잠정 평가했다고 명시한다.

## 최종 보고 기준

- 2 mm와 6 mm는 보정에 포함하지 않은 hold-out 검증이라고 명시한다.
- 별도의 생산 허용오차를 임의로 정하지 않는다.
- 실제 오차를 먼저 제시하고, 영점 이동인지 기울기 오차인지 구분한다.
- 기존 내부 적합 최대오차 약 0.108 mm 및 빈 스테이지 반복 영점 차이 0도 +0.141 mm, 180도 +0.076 mm와 비교한다.
- 보정을 그대로 사용할지, 영점만 조정할지, 선형계수를 다시 적합할지 판단한다.
- 코드 변경이 불필요하면 하지 않는다. 필요한 경우 원본 촬영 데이터와 기존 보정은 보존하고 최소 변경 후 검증한다.
- 생성한 결과와 보고서의 절대경로를 사용자에게 제공한다.

