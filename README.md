# PRO4500 XIMEA UV 캡처 제어

이 작업 공간은 Windows용 PRO4500 / LightCrafter 4500 제어 앱과,
XIMEA UV 카메라를 xiAPI로 사용하는 구조광 캡처 컨트롤러를 포함합니다.
이전의 네트워크/모바일 카메라 기반 캡처 경로는 이 프로젝트에서 사용하지
않습니다.

## 출처 및 공통 모듈

이 작업 공간의 초기 PRO4500/LightCrafter 4500 제어 뼈대는
[lee-lab-skku/PRO4500_Control_System](https://github.com/lee-lab-skku/PRO4500_Control_System)을
바탕으로 확장되었습니다.

두 작업 공간에서 공통으로 사용하는 LightCrafter 4500/DLPC350 라이트엔진
제어 코드는 [eriverOoO/PRO4500_CONTROL](https://github.com/eriverOoO/PRO4500_CONTROL)
저장소로 분리했고, 이 저장소에서는 `GUI/` 경로의 Git submodule로 참조합니다.
XIMEA UV 카메라 연동과 스캔 워크플로 코드는 이 작업 공간에 남겨 둡니다.

## 주요 기능

- USB HID를 통한 LightCrafter 4500 Blue LED 밝기 제어.
- 이미지 파일 폴더를 이용한 패턴 투사.
- `CameraInterface`와 `CameraProvider`를 통한 카메라 추상화.
- 실행 시 XIMEA xiAPI를 사용하는 `XimeaUvCamera` 구현.
- 실제 하드웨어 없이 UI, 저장 흐름, 스캔을 시험할 수 있는 `MockCamera`
  대체 경로.
- 장치 인덱스, 노출, 게인, 트리거 모드, FPS, 이미지 형식, 제한 시간,
  너비, 높이를 설정할 수 있는 XIMEA 카메라 옵션.
- UV 촬영에 맞춘 mono/grayscale 캡처 우선 처리와, 호환성을 위한 선택적
  `rgb24` 변환.
- 미리보기, 단일 캡처, 연속 캡처, 스캔 캡처, 이미지 저장, JSON/CSV 로그.
- 디코더에서 바로 사용할 수 있는 스캔 폴더 구조와 고정 0..21 패턴 계약:
  White, Black, Gray0..Gray7, Sine_000..Sine_270, Gray0_inv..Gray7_inv.
- 패턴별 다중 노출 HDR 캡처. 원본 브래킷 프레임은 `exposures/` 아래에
  보존되고, 병합된 디코드 이미지는 `pattern_000.png` ...
  `pattern_021.png`로 저장되며, 포화/암부 마스크는 `hdr_masks/` 아래에
  저장됩니다.
- 기준/물체 스캔 메타데이터: 프로젝터 기울기, 초점 확인, Scheimpflug 확인,
  리그/캘리브레이션 ID, 프로젝터 밝기, 키스톤 사전 보정 상태.

## 파일 구성

- `StructuredLightControlPanel.cpp`: LED 제어, 투사/캡처 실행, 카메라 설정을
  담당하는 네이티브 Win32 제어 패널.
- `structured_light_pc_controller.py`: 패턴 표시와 카메라 캡처 루프.
- `camera_provider.py`: `CameraInterface`, `CameraProvider`, `XimeaUvCamera`,
  `MockCamera` 구현.
- `camera_config.json`: 카메라 설정 파일. 카메라 값을 코드에 직접 넣지 말고
  이 파일을 수정하세요.
- `build_native_control_panel.bat`: `StructuredLightControlPanel.exe` 빌드.
- `prepare_pc_python_env.ps1`: `.venv-pc` 생성 및 Python 패키지 설치.
- `build.bat` / `PRO4500.cpp`: 기존의 간단한 PRO4500 투사/LED 유틸리티.
- `GUI/`: 빌드에 사용되는 TI LightCrafter 4500 API와 HIDAPI 소스.
- `generated_patterns/`: mock 테스트와 스캔 테스트용 샘플 패턴 이미지.

## XIMEA SDK 요구 사항

다음 항목이 포함된 XIMEA Windows Software Package를 설치해야 합니다.

- XIMEA USB/PCIe 카메라 드라이버.
- 카메라 인식 확인용 XIMEA CamTool 또는 xiCOP.
- xiAPI 런타임 DLL. 일반적으로 `xiapi64.dll`입니다.

`ximea` provider를 사용하기 전에 XIMEA CamTool 또는 xiCOP에서 카메라가
보이는지 확인하세요. 컨트롤러가 런타임을 찾지 못하면, SDK 설치 또는
`camera_config.json`의 `camera.ximea.dll_path` 설정을 안내하는 메시지를
출력하고 정상 종료합니다.

일반적인 `camera_config.json`의 XIMEA 섹션 예시는 다음과 같습니다.

```json
{
  "camera": {
    "provider": "ximea",
    "ximea": {
      "device_index": 0,
      "dll_path": "",
      "exposure_us": 10000,
      "gain_db": 0.0,
      "fps": 15.0,
      "trigger_mode": "software",
      "image_format": "mono8",
      "timeout_ms": 5000,
      "width": 0,
      "height": 0
    }
  },
  "capture": {
    "hdr": {
      "enabled": true,
      "output_bit_depth": 8,
      "saturated_threshold": 250,
      "dark_threshold": 5,
      "brackets": [
        { "name": "short", "exposure_us": 2500, "gain_db": 0.0 },
        { "name": "mid", "exposure_us": 10000, "gain_db": 0.0 },
        { "name": "long", "exposure_us": 40000, "gain_db": 0.0 }
      ]
    },
    "metadata": {
      "scan_type": "object",
      "projector_tilt_deg": 30.0,
      "focus_confirmed": false,
      "scheimpflug_confirmed": false,
      "rig_id": "",
      "calibration_id": "",
      "projector_brightness": "",
      "keystone_predistortion": false
    }
  }
}
```

`dll_path`가 비어 있으면 컨트롤러는 일반적인 XIMEA 설치 경로를 먼저 찾고,
그다음 시스템 `PATH`를 확인합니다. `XIMEA_XIAPI_DLL` 환경 변수에 DLL 경로를
직접 지정할 수도 있습니다.

## 동기화 방식

권장 기본값은 마스터 PC에서 수행하는 소프트웨어 수준 동기화입니다.

1. PC가 프로젝터 화면에 패턴 하나를 표시합니다.
2. PC가 `settle_ms`만큼 대기합니다.
3. PC가 xiAPI를 통해 XIMEA `trigger_software`를 보냅니다.
4. PC가 `xiGetImage`를 기다린 뒤 프레임을 저장하고 메타데이터를 기록한 다음
   다음 패턴으로 넘어갑니다.

이 방식은 다음 설정과 함께 사용합니다.

```json
"trigger_mode": "software"
```

마스터 장비 또는 프로젝터 트리거 소스에서 XIMEA 카메라 입력으로 하드웨어
트리거 케이블을 연결했다면 다음 설정을 사용합니다.

```json
"trigger_mode": "edge_rising"
```

또는 다음 설정을 사용할 수 있습니다.

```json
"trigger_mode": "edge_falling"
```

하드웨어 트리거 모드에서는 컨트롤러가 `trigger_software`를 내보내지 않습니다.
대신 acquisition을 시작한 뒤 외부 edge가 들어와 `xiGetImage`가 프레임을
반환할 때까지 기다립니다. 트리거 간격보다 충분히 긴 `timeout_ms` 값을
사용하세요.

`trigger_mode: "off"` 또는 `"freerun"`은 미리보기나 비동기 테스트에만
사용하는 것을 권장합니다.

## Mock 카메라

기본 설정은 다음 provider를 사용합니다.

```json
"provider": "mock"
```

이 설정은 mono gradient 프레임을 생성하므로 실제 카메라 없이도 미리보기,
캡처, 스캔 로그, 이미지 저장 흐름을 테스트할 수 있습니다.

## 설정 방법

1. 네이티브 제어 패널을 빌드하려면 MSYS2 MinGW-w64를 설치합니다.
2. Python 환경을 준비합니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\prepare_pc_python_env.ps1
```

3. 네이티브 제어 패널을 빌드합니다.

```bat
build_native_control_panel.bat
```

4. 실행합니다.

```bat
run_control_panel.bat
```

## 명령줄 예시

Mock 단일 캡처:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --single-capture --camera-provider mock
```

XIMEA 실시간 미리보기:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --preview --camera-provider ximea
```

미리보기 창을 열지 않고 XIMEA SDK/장치 연결 확인:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --check-camera --camera-provider ximea
```

XIMEA 구조광 스캔:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --camera-provider ximea --patterns .\generated_patterns --output .\captures --angles 0
```

하드웨어 없이 합성 데이터로 디코더 폴더를 시험:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --dry-run --patterns .\generated_patterns --output .\captures --scan-type reference --angles 0
```

기준/물체 메타데이터 예시:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --camera-provider ximea --scan-type reference --rig-id uv_rig_01 --calibration-id calib_20260706 --focus-confirmed --scheimpflug-confirmed
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --camera-provider ximea --scan-type object --rig-id uv_rig_01 --calibration-id calib_20260706 --focus-confirmed --scheimpflug-confirmed
```

기존 도구용 14패턴 레거시 스캔:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --camera-provider ximea --legacy-14-patterns --patterns .\generated_patterns --output .\captures --angles 0
```

Mock 카메라 투사/로그 테스트:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --camera-provider mock --patterns .\generated_patterns --output .\captures --angles 0 --windowed
```

## 출력

캡처 결과는 다음 위치에 저장됩니다.

```text
captures/<scan_id>/
```

스캔 모드는 다음 파일을 기록합니다.

- 최종 디코더 이미지: `pattern_000.png` ... `pattern_021.png`.
- 원본 브래킷 프레임: `exposures/pattern_002/short.png`,
  `exposures/pattern_002/mid.png`, `exposures/pattern_002/long.png` 등.
- HDR 마스크: `hdr_masks/pattern_002_saturated.png`,
  `hdr_masks/pattern_002_dark.png` 등.
- `scan_log.json`
- `hdr_merge_report.json`
- `scan_log.csv`

`scan_log.json`에는 고정 패턴 ID/라벨 계약, 최종 파일명, 브래킷 파일명,
노출/게인 값, 카메라 프레임 메타데이터, HDR 임계값, 병합 통계,
기준/물체 리그 메타데이터가 기록됩니다. 컨트롤러는 스캔 완료 전에
예상되는 모든 최종 패턴 ID가 존재하는지 검증합니다.

단일/연속 캡처 모드는 이미지와 `capture_log.json`이 들어 있는 캡처 폴더를
생성합니다.

## 참고 사항

- UV 기본값으로는 `mono8`을 권장합니다. 더 높은 bit depth가 필요하고 후속
  도구가 16-bit 이미지를 처리할 수 있다면 `mono16`을 사용하세요.
- `trigger_mode`는 `off`, `software`, `edge_rising`, `edge_falling`을
  지원합니다.
- FPS 제어는 xiAPI를 통해 요청됩니다. 일부 XIMEA 모델은 노출, ROI, 대역폭,
  카메라 제품군에 따라 특정 값을 거부할 수 있습니다. 이 경우 가능한 한
  캡처를 계속 진행하면서 경고 로그를 남깁니다.
- 제어 패널은 Python 컨트롤러를 자식 프로세스로 실행하고, 해당 로그 출력을
  메인 창에 표시합니다.
## Save Policy

The native control panel includes a `Save All` option.

- Off: save only final decoder images, `pattern_000.png` ... `pattern_021.png`.
- On: also save raw exposure brackets under `exposures/` and HDR masks under `hdr_masks/`.

For one scan angle, `Save All` off writes 22 PNG images. `Save All` on writes
132 PNG images: 22 final decoder images, 66 raw bracket images, and 44 HDR mask
images.

CLI equivalent:

```powershell
.\.venv-pc\Scripts\python.exe .\structured_light_pc_controller.py --dry-run --save-all-images --patterns .\generated_patterns --output .\captures --scan-type reference --angles 0
```
