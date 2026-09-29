# 촬영·계산 PC용 실행 프롬프트

아래 블록 전체를 촬영 및 PCB_FPP_Decoder 계산을 수행하는 PC의 Codex에
붙여 넣으세요.

---

당신은 `PRO4500_CONTROL_ximea`와 `Non-planar_calc` 저장소가 있고, 기존
촬영 원본과 디코드 산출물에 접근할 수 있는 Windows PC에서 작업한다.

목표는 현재 phase-linear 높이 오차의 주원인이 방향별 전역 스칼라
`phase_per_mm`인지, 아니면 12 projector-pixel 주기의 위상/감마 오차인지
기존 데이터로 분리 검증하는 것이다. 새 촬영은 기존 배열과 원본이
없을 때만 검토한다.

반드시 `PRO4500_CONTROL_ximea/PHASE_LINEAR_SPATIAL_EXPERIMENT_KO.md`를 먼저
전체 읽고 그 절차를 따라라. 분석 코드는
`scripts/phase_linear_spatial_experiment.py`, 입력 예시는
`scripts/phase_linear_spatial_manifest.example.json`, 실행기는
`run_phase_linear_spatial_experiment.ps1`이다.

다음 순서로 작업하라.

1. Git 상태와 두 저장소의 실제 경로를 확인하라. 기존 사용자 변경은
   유지하고, 생산용 보정 NPZ나 기준 위상 파일을 덮어쓰지 마라.
2. 기존 2.95, 5.03, 7.96 mm 보정 스캔과 2.07, 6.10 mm hold-out
   스캔을 찾아라. 각 스캔에서 물리적 0°/180°가 어느 폴더인지
   ArUco 배치, 기존 보고서와 스캔 로그로 확정하라. `angle_000` 또는
   `deg_0`라는 이름만 보고 물리 0°로 가정하지 마라.
3. 각 방향의 `height/delta_phase.npy`, `phase/absolute_phase.npy`,
   `masks/combined_mask.png`를 찾아라. `delta_phase.npy`가 없으면 같은
   설치 조건의 물리 0°/180° 기준 `absolute_phase.npy`를 찾아 매니페스트의
   `reference_phase_by_physical_view`에 지정하라.
4. 원본 스캔만 남아 있다면 새로 촬영하지 말고, 당시와 동일한
   기준면·phase axis·swapped convention·half-period correction·threshold로
   다시 디코드하라. 재디코드 설정을 결과 보고에 기록하라.
5. 예시 매니페스트를 복사해 저장소 루트의
   `phase_linear_spatial_manifest.json`을 작성하라. 이 파일은 Git ignore
   대상이다. `baseline_phase_per_mm_by_physical_view`는 기존 보정값
   0°=5.882307709, 180°=5.836247840 rad/mm로 유지하라.
6. 매 스캔·방향의 블록 상면 ROI를 외곽에서 20~25 px 안쪽으로
   지정하라. 정확한 상면 마스크가 있으면 `surface_mask`도 지정하라.
   ROI나 마스크 없이 전체 프레임을 보정 표본으로 사용하지 마라.
7. 먼저 다음 preflight를 실행하고 모든 경로, 방향, 크기, 유효
   픽셀 수를 확인하라.

   ```powershell
   .\run_phase_linear_spatial_experiment.ps1 `
     -Manifest .\phase_linear_spatial_manifest.json `
     -OutputDir .\phase_linear_spatial_result `
     -CheckOnly
   ```

8. preflight가 통과하면 `-CheckOnly`를 빼고 실행하라. 산출물은 기존
   생산 보정과 분리된 `phase_linear_spatial_result`에만 저장하라.
9. JSON 보고서에서 다음을 방향별·hold-out 높이별로 표로 정리하라.

   - 서로 다른 높이 2개 이상이 중첩된 픽셀 수와 직접 픽셀 기울기
     P10/P50/P90, P10~P90 전체 폭(%)
   - 기존 스칼라 대비 공간 모델의 median error, MAE, RMSE, P95 absolute error
   - MAE와 P95 개선율
   - 스칼라/공간 모델 오차의 phase-fold first-harmonic peak-to-peak mm와 R²
   - 2차 모델의 외삽 clipping 픽셀 수와 블록이 실제로 덮은 영역

판정 원칙은 다음과 같다.

- 보정에 쓴 입력의 in-sample 개선은 근거로 삼지 마라.
- 2.07 mm와 6.10 mm, 0°와 180° 모두에서 MAE와 P95가 일관되게
  줄어야 위치 의존 기울기 모델이 지지된다.
- phase-fold 주기 진폭이 공간 보정 후에도 반복적으로 남고 R²가 유의하면
  감마/위상 주기 성분을 별도 원인으로 보고하라.
- 한 높이나 한 방향만 개선되면 생산용 보정을 교체하지 마라.
- 생성된 NPZ는 실험용이다. 결과 검토 전에 기존 GUI/생산 설정에
  등록하지 마라.

기존 디코드 배열과 원본이 모두 부족해 추가 촬영이 필요하면 즉시 촬영하지
말고, 누락된 데이터와 필요한 촬영 목록을 먼저 보고하라. 촬영은 빈
스테이지, 2.95/5.03/7.96 mm 보정 높이, 2.07/6.10 mm hold-out을
같은 XY 위치와 문제 봉우리 영역 `(x 약 800~900, y 약 550~620)`을 포함하는
별도 위치에서 수행해야 한다. 모든 스캔은 물리 0° 복귀, 동일 초점·노출·
12 px 패턴·기준면을 유지해야 한다. mono12, HDR, 8-step은 이 검증
이후에 근거가 남을 때만 별도 실험으로 제안하라.

마지막 보고에는 반드시 다음을 포함하라.

- 사용한 스캔/기준면/매니페스트/출력 절대 경로
- 물리 방향 교환이 필요했던 스캔 목록과 판정 근거
- 스칼라 대비 공간 보정 개선 표
- phase-fold 결과와 감마 원인 유지/기각 판정
- 생산 보정 교체 권고 여부와 근거
- 새 촬영 필요 여부; 필요하면 정확한 촬영 목록과 순서
- 실행 중 생성하거나 변경한 파일 목록

---
