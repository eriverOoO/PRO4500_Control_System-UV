# Phase-linear 위치 의존성 검증 인계서

## 먼저 확인된 사실

현재 이 저장소의 `calibration_output/phase_linear_20260909` 번들은 방향별
`phase_per_mm`를 **스칼라 한 개**로 저장합니다. NPZ에는
`paired_slope_p10_p90`이 들어 있지 않습니다. 구간값은 계산 스크립트가 만드는
JSON 보고서 항목이며, 2026-09-09 번들의 구형 보고서에는 촬영 대표값으로 만든
`pairwise_slope_p10_p90_abs`만 있습니다.

그 값은 물리적 0도에서 5.7687~6.2337 rad/mm, 물리적 180도에서
5.8594~6.1993 rad/mm입니다. 각각 중앙값 대비 전체 폭이 약 7.8%, 5.7%라서
공간 감도 오차를 의심할 이유는 충분합니다. 그러나 이 폭에는 촬영 반복 차이,
블록 평탄도, ROI 차이와 높이 비선형성도 섞이므로 **그 수치만으로 픽셀별 감도
차이가 입증되지는 않습니다.** 독립 hold-out 개선 여부가 최종 판정 기준입니다.

이 PC에 보존된 5.02 mm 5회분은 한 높이뿐이므로 픽셀별 기울기를 적합할 수
없습니다. 새 촬영이 반드시 필요한 것은 아닙니다. 촬영/계산 PC에 기존
2.95/5.03/7.96 mm의 `delta_phase.npy`, 정확한 블록 상면 ROI(또는 마스크),
2.07/6.10 mm hold-out의 같은 파일이 남아 있으면 아래 실험을 바로 실행할 수
있습니다.

## 계산 PC에서 실행

1. `scripts/phase_linear_spatial_manifest.example.json`을 복사해 실제 경로와
   ROI로 수정합니다. 각 `views.0`과 `views.180`은 폴더 이름이 아니라 **물리적
   방향**입니다. 촬영 시작 방향이 바뀐 스캔은 예시처럼 `subdir`을 서로
   교환합니다.
   `delta_phase.npy`가 없으면 `reference_phase_by_physical_view`에 0°/180°
   기준 `absolute_phase.npy`를 지정합니다. 스크립트가 각 스캔의
   `absolute_phase.npy`에서 기준을 빼 `delta_phase`를 재구성합니다.
   `baseline_phase_per_mm_by_physical_view`는 현재 사용 중인 스칼라
   5.882307709 / 5.836247840 rad/mm로 유지합니다. hold-out의
   스칼라 비교값은 이 값에 고정됩니다.
2. ROI는 블록 외곽을 20~25 px 안쪽으로 줄인 상면만 지정합니다. 더 정확한
   상면 마스크가 있으면 `surface_mask`도 함께 지정합니다. 둘을 함께 쓰면
   교집합만 사용합니다.
3. 먼저 파일과 방향 입력을 점검합니다.

```powershell
Copy-Item .\scripts\phase_linear_spatial_manifest.example.json .\phase_linear_spatial_manifest.json
.\run_phase_linear_spatial_experiment.ps1 `
  -Manifest .\phase_linear_spatial_manifest.json `
  -OutputDir D:\phase_linear_spatial_trial `
  -CheckOnly
```

4. 점검이 통과하면 `-CheckOnly`를 빼고 실행합니다.

```powershell
.\run_phase_linear_spatial_experiment.ps1 `
  -Manifest .\phase_linear_spatial_manifest.json `
  -OutputDir D:\phase_linear_spatial_trial
```

스크립트는 촬영이나 장치 제어를 하지 않습니다. 디코더가 만든 배열만 읽습니다.

## 출력과 판정

- `phase_linear_spatial_experiment_report.json`: 스칼라 대비 공간 모델의
  hold-out MAE/P95 변화와 wrapped-phase 진단
- `phase_linear_spatial_calibration.npz`: PCB_FPP_Decoder `phase_linear` 모드가
  바로 읽을 수 있는 방향별 2차 기울기 맵
- `slope_observed_deg_0.npy`, `slope_observed_deg_180.npy`: 두 개 이상의 서로
  다른 높이가 같은 카메라 픽셀을 덮은 곳에서만 계산한 직접 픽셀 적합값
- `slope_height_level_count_*.npy`: 각 픽셀에 실제로 겹친 서로 다른 높이 수
- `slope_model_*.png`: 저차 공간 모델 미리보기
- `phase_fold_*.csv`: 12 px 무늬 한 주기에 대응하는 wrapped phase별 오차

판정은 다음 순서로 합니다.

1. `direct_pixel_slope.p10_p90_full_width_percent_of_median`으로 실제 중첩
   픽셀의 감도 폭을 확인합니다. 중첩 높이가 부족하면 이 값보다
   `pixels_by_distinct_height_level_count`를 먼저 봅니다.
2. 공간 모델이 **독립 hold-out의 두 높이와 두 방향 모두**에서 MAE와 P95를
   낮추는지 확인합니다. 보정 입력의 in-sample 오차 감소는 증거로 쓰지 않습니다.
3. `phase_fold_*`의 `first_harmonic_peak_to_peak_mm`과 R2가 공간 보정 후에도
   반복해서 남으면 감마/위상 주기 성분이 별도로 있다는 뜻입니다. 진폭이 작고
   R2도 작으면 감마는 주원인 후보에서 내릴 수 있습니다.
4. 한 방향 또는 한 높이만 좋아지거나 외삽 영역에서 부호가 바뀌면 생산 보정을
   교체하지 않습니다. 블록이 덮지 않은 영역은 2차 다항식 외삽이므로 별도
   위치 hold-out이 필요합니다.

## 기존 배열이 없을 때만 촬영/재계산 PC에서 할 일

새 촬영에 앞서 원본 스캔이 남아 있으면 동일 기준면과 동일 디코더 설정으로
다시 디코드해 `delta_phase.npy`, `absolute_phase.npy`, `combined_mask.png`를
만듭니다. 이것만으로 충분하면 재촬영하지 않습니다.

원본도 없을 때에만 다음을 다시 촬영합니다.

- 빈 스테이지 기준면 1회 이상
- 2.95, 5.03, 7.96 mm 블록을 같은 XY 위치에서 각 0°/180°
- 보정에 절대 넣지 않을 2.07, 6.10 mm hold-out을 중앙과 봉우리 영역
  `(x 약 800~900, y 약 550~620)`을 포함하도록 위치를 바꿔 촬영
- 매 스캔 전에 물리적 0°로 복귀하고 ArUco 배치로 방향을 기록
- 카메라/프로젝터 위치, 초점, 노출, 패턴 12 px, 기준면을 전 촬영에서 고정

이 실험에는 mono12 전환이나 8-step 패턴이 선행 조건이 아닙니다. 먼저 현재
mono8/4-step 데이터로 공간 모델과 위상 주기 성분을 분리한 뒤, 포화 또는 양자화
근거가 남을 때만 촬영 조건을 바꾸는 편이 비교 가능성을 보존합니다.
