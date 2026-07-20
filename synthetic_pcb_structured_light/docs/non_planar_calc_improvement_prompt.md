# Prompt for the Non-planar_calc task

아래 작업을 `C:\Users\LEELAB\Desktop\Non-planar_calc`에서 수행하라.

## 목표

PCB FPP 디코더가 제공된 합성 object/reference 스캔으로부터 0~3 mm 높이를 양의 mm 단위로 복원하도록 개선한다. 기존 사용자의 수정사항을 보존하고, 먼저 `git status --short`와 관련 코드를 읽은 다음 구현·테스트·수치 검증까지 완료한다.

## 입력 데이터

- Object 0도: `C:\Users\LEELAB\Desktop\PRO4500_CONTROL_ximea\synthetic_pcb_structured_light\output\angle_000`
- Object 180도: `C:\Users\LEELAB\Desktop\PRO4500_CONTROL_ximea\synthetic_pcb_structured_light\output\angle_180`
- Reference 0도: `C:\Users\LEELAB\Desktop\PRO4500_CONTROL_ximea\synthetic_pcb_structured_light\output\reference\angle_000`
- Reference 180도: `C:\Users\LEELAB\Desktop\PRO4500_CONTROL_ximea\synthetic_pcb_structured_light\output\reference\angle_180`
- Ground truth 0도: `C:\Users\LEELAB\Desktop\PRO4500_CONTROL_ximea\synthetic_pcb_structured_light\output\ground_truth\angle_000_height_mm.npy`
- Ground truth 180도: `C:\Users\LEELAB\Desktop\PRO4500_CONTROL_ximea\synthetic_pcb_structured_light\output\ground_truth\angle_180_height_mm.npy`
- 합성 보정값: `C:\Users\LEELAB\Desktop\PRO4500_CONTROL_ximea\synthetic_pcb_structured_light\configs\synthetic_decoder_calibration.json`
- Object/reference manifest도 각각 읽어서 frame mapping, bit depth, 카메라·프로젝터 설정을 검사하라.

Reference 데이터는 PCB를 평탄화한 영상이 아니다. PCB를 스테이지에서 완전히 제거한 뒤 균일한 무광 평면 스테이지에 동일한 패턴을 투사하여 촬영한 조건이다. Reference 영상에 PCB 텍스처, 패드, 배선 또는 부품 형상이 없어야 하며, object 영상과 동일한 카메라·프로젝터 좌표계를 사용한다.

Ground truth는 테스트 및 정확도 리포트에만 사용한다. 일반 decode 경로에서 정답 높이를 입력으로 사용하거나 결과에 복사하면 안 된다.

## 이미 확인된 원인과 기준 수치

1. 현재 GUI 기본 `height_mode=relative`는 mm 높이가 아니라 absolute phase를 height로 표시한다.
2. GUI 기본 `detrend=True`는 부품까지 포함해 평면을 맞추므로 0 mm 기판 기준을 편향시킨다.
3. 실제 BMP 사인 패턴은 cosine 계열이다. 이 데이터에는 `phase_convention=swapped`, 즉 `atan2(I270-I90, I000-I180)`가 맞다.
4. 생성기에서 높이가 증가하면 projector x phase가 감소한다. `delta_phi = phi_object - phi_reference`에 `height_sign=-1`을 적용해야 양의 높이가 된다.
5. 검은 IC는 기본 `min_signal=20`에서 많이 제거된다. 합성 데이터 기본은 5가 적절하지만 수동 설정은 유지하고 자동 임계값을 도입한다면 진단값과 선택 근거를 보고서에 남겨라.
6. 0/180 각도에 같은 reference를 재사용하지 말고 각 view에 대응하는 reference를 따로 디코딩해야 한다.
7. 현재 fusion은 두 view가 한 주기 이상 불일치해도 평균한다. fusion 전에 cycle/height consistency 검사를 추가해야 한다.

수정 전 읽기 전용 재현 결과:

- relative+detrend 결과와 GT 상관: 약 `-0.865`
- 상대 phase 배율: 약 `-23.38 phase/mm`
- `min_signal=20`에서 1 mm 이상 부품 fusion 유효률: 약 `70.0%`
- `min_signal=5`에서 1 mm 이상 부품 fusion 유효률: 약 `99.4%`

각도별 empty-stage reference를 사용하고 `swapped`, `height_sign=-1`, `min_signal=5`, `detrend=False`로 계산한 새 기준은 다음과 같다. 이전의 PCB 반사율을 그대로 유지한 flat-reference 기준값은 사용하지 않는다.

경계 보정 전:

- GT 상관: `0.99862`
- phase 대 height 선형 기울기: `23.220258 phase/mm`
- phase offset: `-0.009285 phase`
- 선형 mm 변환 후 MAE: `0.00932 mm`
- RMSE: `0.03648 mm`
- 전체 P95 absolute error: `0.12620 mm`

현재 heuristic half-period correction 적용 후:

- GT 상관: `0.999523`
- phase 대 height 선형 기울기: `23.224349 phase/mm`
- phase offset: `-0.011647 phase`
- MAE: `0.00436 mm`
- RMSE: `0.02144 mm`
- 전체 P95 absolute error: `0.00822 mm`
- 전체 P99 absolute error: `0.13431 mm`
- 부품 영역 P95 absolute error: `0.13475 mm`
- 최대 absolute error: `0.53511 mm`
- 유효 PCB 비율: `99.49%`
- 1 mm 이상 부품 유효 비율: `99.31%`

전체 P95가 좋아도 부품 영역과 P99에는 반주기/cycle-slip 계열 이상치가 남아 있다. 전체 통계만 보고 완료 처리하지 말고 부품 영역 통계와 rejection mask를 함께 검사하라.

## 구현 요구사항

1. `DecodeConfig`와 CLI/GUI에 angle별 reference 입력을 추가한다. 예: `reference_scan_0`, `reference_scan_180` 또는 동등하게 명확한 API. 기존 단일 reference 옵션은 하위 호환을 유지한다.
2. 0도 object는 0도 reference와, 180도 object는 180도 reference와 각각 absolute phase subtraction을 수행한 다음 공간 정합 및 fusion을 수행한다.
3. `phase_linear` metric height mode를 추가한다. 보정 JSON의 `phase_per_mm`, `offset_phase`, `height_sign`을 읽고 다음 식을 사용한다.

   `signed_delta = height_sign * (phi_object - phi_reference)`

   `height_mm = (signed_delta - offset_phase) / phase_per_mm`

   출력 units는 반드시 `mm`, metric은 `true`로 기록한다.
4. `relative` 모드 결과는 height라고 오해하지 않도록 UI·리포트·그림 제목에 `relative phase`와 `phase units`를 명시한다. relative 모드에서는 mm 컬러바를 표시하지 않는다.
5. 이 실제 BMP 세트에 맞는 `swapped` convention을 설정에서 명시적으로 선택할 수 있게 유지하고, 사인 4장의 첫 행/중앙 행으로 convention 적합성을 진단하는 기능을 추가한다. 자동 선택을 구현한다면 선택된 convention과 score를 report에 남긴다.
6. phase가 `2*pi`에 매우 가까운 값을 0 경계로 안정적으로 처리한다. Gray period 경계와 sine wrap의 정렬을 검사하고 cycle slip mask를 출력한다. 기존 heuristic half-period correction은 전체 P95를 개선하지만 부품 P95 약 `0.135 mm`의 잔여 이상치를 남기므로, correction 전후의 오류와 mask 정밀도/재현율을 별도로 검증한다.
7. fusion overlap에서 두 metric height의 차이가 설정 임계값보다 크거나 cycle slip으로 판정되면 무조건 가중 평균하지 않는다. 더 높은 confidence view를 선택하거나 해당 픽셀을 invalid 처리하고 rejection mask를 저장한다.
8. `min_signal`은 기존 수동값을 보존한다. 합성 데이터 검증 프리셋은 5를 사용한다. 자동 모드를 추가할 경우 White-Black signal 분포 기반으로 결정하고 보고서에 threshold와 valid ratio를 기록한다.
9. reference를 쓰는 metric 모드에서는 일반 object 픽셀 전체에 대한 least-squares detrend를 기본 적용하지 않는다. 필요하면 reference subtraction 후 기판 후보에만 robust/RANSAC 평면 보정을 별도 옵션으로 제공한다.
10. 16-bit mono PNG가 로더에서 선형 0~255 계산 범위로 변환되는 기존 동작은 유지한다.
11. 기존 API와 테스트를 깨지 말고 새로운 단위 테스트와 합성 데이터 통합 테스트를 추가한다.

## 검증 및 산출물

다음 결과를 별도 검증 출력 폴더에 저장하라.

- `height_mm.npy`
- mm 컬러바를 가진 `height_mm.png`
- `error_mm.npy`
- `error_mm.png`
- `cycle_slip_mask.png`
- `fusion_rejection_mask.png`
- `accuracy_report.json`
- 0도/180도/fused 비교 overview

`accuracy_report.json`에는 전체 PCB, 평평한 기판, 부품, 1 mm 이상 부품별로 valid ratio, bias, MAE, RMSE, median absolute error, P95 absolute error, max error를 기록한다.

완료 기준:

- 출력 높이 단위가 mm이고 높은 부품이 양수다.
- PCB 유효률 99% 이상, 1 mm 이상 부품 유효률 98% 이상이다.
- P95 absolute error 0.05 mm 이하를 우선 목표로 한다.
- 부품 영역 P95 absolute error도 0.05 mm 이하를 목표로 하며, 전체 P95만으로 통과 처리하지 않는다.
- cycle-slip 이상치를 포함한 전체 RMSE는 0.10 mm 이하를 목표로 한다.
- object/reference 파일을 바꿔 넣거나 ground truth를 decode 입력으로 사용해서 수치를 맞추지 않는다.
- `pytest` 전체가 통과한다.
- 변경 파일, 실행 명령, 정확도 수치, 남은 위험을 최종 보고한다.

추가 확인 질문 없이 현재 코드베이스의 패턴과 스타일을 따라 구현을 시작하라.
