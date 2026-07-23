#define UNICODE
#define _UNICODE

#include <windows.h>
#include <commctrl.h>
#include <gdiplus.h>

#include <algorithm>
#include <atomic>
#include <filesystem>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "GUI/dlpc350_common.h"
#include "GUI/dlpc350_api.h"
#include "GUI/dlpc350_usb.h"
#include "projector_usb_diagnostics.h"

namespace fs = std::filesystem;
using namespace Gdiplus;

namespace {

constexpr int IDC_BLUE_SLIDER = 1001;
constexpr int IDC_BLUE_VALUE  = 1002;
constexpr int IDC_APPLY       = 1003;
constexpr int IDC_LED_OFF     = 1004;
constexpr int IDC_PROJECT     = 1005;
constexpr int IDC_STOP        = 1006;
constexpr int IDC_FOLDER      = 1007;
constexpr int IDC_EXPOSURE    = 1008;
constexpr int IDC_DARK        = 1009;
constexpr int IDC_REPEAT      = 1010;
constexpr int IDC_DISPLAY     = 1011;
constexpr int IDC_STATUS      = 1012;

HWND g_mainWindow = nullptr;
HWND g_slider = nullptr;
HWND g_valueLabel = nullptr;
HWND g_statusLabel = nullptr;
std::mutex g_usbMutex;
std::atomic_bool g_stopProjection{false};
std::atomic_bool g_projectionRunning{false};
ULONG_PTR g_gdiplusToken = 0;

struct MonitorInfo {
    RECT rect{};
};

struct ProjectionState {
    std::vector<std::wstring> images;
    int exposureMs = 1000;
    int darkMs = 150;
    int repeat = 1;
    int displayIndex = 1;
};

struct ProjectionWindowData {
    ProjectionState state;
    size_t imageIndex = 0;
    int repeatIndex = 0;
    bool dark = false;
    std::unique_ptr<Image> image;
};

std::wstring get_exe_dir() {
    std::vector<wchar_t> buffer(MAX_PATH);
    DWORD length = GetModuleFileNameW(
        nullptr,
        buffer.data(),
        static_cast<DWORD>(buffer.size()));
    while (length == buffer.size()) {
        buffer.resize(buffer.size() * 2);
        length = GetModuleFileNameW(
            nullptr,
            buffer.data(),
            static_cast<DWORD>(buffer.size()));
    }
    const fs::path executable(std::wstring(buffer.data(), length));
    return executable.parent_path().wstring();
}

std::wstring path_join(const std::wstring& base, const std::wstring& child) {
    return (fs::path(base) / child).wstring();
}

std::wstring window_text(HWND control) {
    const int length = GetWindowTextLengthW(control);
    std::wstring text(static_cast<size_t>(length), L'\0');
    GetWindowTextW(control, text.data(), length + 1);
    return text;
}

int window_int(HWND control, int fallback, int minimum) {
    try {
        return std::max(minimum, std::stoi(window_text(control)));
    } catch (...) {
        return fallback;
    }
}

void set_status(const std::wstring& text) {
    SetWindowTextW(g_statusLabel, text.c_str());
}

bool connect_projector(std::wstring& error) {
    if (DLPC350_USB_Init() != 0) {
        error = DLPC350_USB_LastError();
        if (error.empty()) error = L"HIDAPI initialization failed";
        return false;
    }
    if (DLPC350_USB_Open() != 0) {
        error = DLPC350_USB_LastError();
        DLPC350_USB_Exit();
        if (error.empty()) {
            error = L"LightCrafter 4500 not found or cannot be opened (VID 0451, PID 6401)";
        }
        return false;
    }
    return true;
}

void disconnect_projector() {
    DLPC350_USB_Close();
    DLPC350_USB_Exit();
}

bool set_blue_led(int brightness, std::wstring& error) {
    std::lock_guard<std::mutex> lock(g_usbMutex);
    // TI LightCrafter 4500 reference GUI uses inverted LED-current values:
    // register 255 = minimum, register 0 = maximum.
    const unsigned char current = static_cast<unsigned char>(255 - std::clamp(brightness, 0, 255));
    constexpr int commandAttempts = 2;
    for (int attempt = 1; attempt <= commandAttempts; ++attempt) {
        if (!connect_projector(error)) {
            return false;
        }

        const int enableResult = DLPC350_SetLedEnables(false, false, false, brightness > 0);
        const int currentResult = enableResult < 0
            ? -1
            : DLPC350_SetLedCurrents(255, 255, current);
        std::wstring usbError = DLPC350_USB_LastError();
        disconnect_projector();

        if (enableResult >= 0 && currentResult >= 0) {
            return true;
        }
        error = usbError.empty() ? L"Blue LED command failed" : usbError;
        if (attempt < commandAttempts) {
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
        }
    }
    error += L" The LED command was retried after reopening the USB device.";
    return false;
}

BOOL CALLBACK collect_monitor(HMONITOR, HDC, LPRECT rect, LPARAM data) {
    auto* monitors = reinterpret_cast<std::vector<MonitorInfo>*>(data);
    monitors->push_back(MonitorInfo{*rect});
    return TRUE;
}

std::vector<MonitorInfo> monitors() {
    std::vector<MonitorInfo> result;
    EnumDisplayMonitors(nullptr, nullptr, collect_monitor, reinterpret_cast<LPARAM>(&result));
    return result;
}

std::vector<std::wstring> image_files(const std::wstring& folder) {
    std::vector<std::wstring> result;
    std::error_code ec;
    for (const auto& entry : fs::directory_iterator(folder, ec)) {
        if (!entry.is_regular_file()) {
            continue;
        }
        std::wstring ext = entry.path().extension().wstring();
        std::transform(ext.begin(), ext.end(), ext.begin(), ::towlower);
        if (ext == L".bmp" || ext == L".png" || ext == L".jpg" ||
            ext == L".jpeg" || ext == L".gif" || ext == L".tif" ||
            ext == L".tiff") {
            result.push_back(entry.path().wstring());
        }
    }
    std::sort(result.begin(), result.end());
    return result;
}

void load_current_image(ProjectionWindowData* data) {
    data->image.reset();
    if (!data->dark && data->imageIndex < data->state.images.size()) {
        data->image = std::make_unique<Image>(data->state.images[data->imageIndex].c_str());
    }
}

LRESULT CALLBACK projection_proc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    auto* data = reinterpret_cast<ProjectionWindowData*>(
        GetWindowLongPtrW(window, GWLP_USERDATA));

    switch (message) {
    case WM_CREATE: {
        const auto* create = reinterpret_cast<CREATESTRUCTW*>(lParam);
        data = reinterpret_cast<ProjectionWindowData*>(create->lpCreateParams);
        SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(data));
        load_current_image(data);
        SetTimer(window, 1, static_cast<UINT>(std::max(1, data->state.exposureMs)), nullptr);
        return 0;
    }
    case WM_TIMER:
        if (!data || g_stopProjection.load()) {
            DestroyWindow(window);
            return 0;
        }
        KillTimer(window, 1);
        if (!data->dark && data->state.darkMs > 0) {
            data->dark = true;
            data->image.reset();
            SetTimer(window, 1, static_cast<UINT>(data->state.darkMs), nullptr);
        } else {
            data->dark = false;
            ++data->imageIndex;
            if (data->imageIndex >= data->state.images.size()) {
                data->imageIndex = 0;
                ++data->repeatIndex;
                if (data->repeatIndex >= data->state.repeat) {
                    DestroyWindow(window);
                    return 0;
                }
            }
            load_current_image(data);
            SetTimer(window, 1, static_cast<UINT>(std::max(1, data->state.exposureMs)), nullptr);
        }
        InvalidateRect(window, nullptr, FALSE);
        return 0;
    case WM_PAINT: {
        PAINTSTRUCT paint{};
        HDC dc = BeginPaint(window, &paint);
        RECT client{};
        GetClientRect(window, &client);
        Graphics graphics(dc);
        graphics.Clear(Color::Black);
        if (data && data->image && data->image->GetLastStatus() == Ok) {
            const double sx = static_cast<double>(client.right) / data->image->GetWidth();
            const double sy = static_cast<double>(client.bottom) / data->image->GetHeight();
            const double scale = std::min(sx, sy);
            const int width = static_cast<int>(data->image->GetWidth() * scale);
            const int height = static_cast<int>(data->image->GetHeight() * scale);
            const int x = (client.right - width) / 2;
            const int y = (client.bottom - height) / 2;
            graphics.SetInterpolationMode(InterpolationModeNearestNeighbor);
            graphics.DrawImage(data->image.get(), x, y, width, height);
        }
        EndPaint(window, &paint);
        return 0;
    }
    case WM_KEYDOWN:
        if (wParam == VK_ESCAPE) {
            DestroyWindow(window);
        }
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, wParam, lParam);
}

void projection_thread(ProjectionState state) {
    const auto displayList = monitors();
    if (displayList.empty()) {
        g_projectionRunning = false;
        PostMessageW(g_mainWindow, WM_APP + 1, 0, 0);
        return;
    }
    const int index = std::clamp(state.displayIndex, 0, static_cast<int>(displayList.size()) - 1);
    const RECT rect = displayList[static_cast<size_t>(index)].rect;

    WNDCLASSW wc{};
    wc.lpfnWndProc = projection_proc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH));
    wc.lpszClassName = L"PRO4500ProjectionWindow";
    RegisterClassW(&wc);

    ProjectionWindowData data{std::move(state)};
    HWND window = CreateWindowExW(
        WS_EX_TOPMOST, wc.lpszClassName, L"PRO4500 Projection", WS_POPUP,
        rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top,
        nullptr, nullptr, wc.hInstance, &data);

    if (window) {
        ShowWindow(window, SW_SHOW);
        SetForegroundWindow(window);
        MSG msg{};
        while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }

    g_projectionRunning = false;
    PostMessageW(g_mainWindow, WM_APP + 1, 0, 0);
}

void start_projection(HWND window) {
    if (g_projectionRunning.exchange(true)) {
        set_status(L"이미 이미지 출력 중입니다.");
        return;
    }

    ProjectionState state;
    state.images = image_files(window_text(GetDlgItem(window, IDC_FOLDER)));
    state.exposureMs = window_int(GetDlgItem(window, IDC_EXPOSURE), 1000, 1);
    state.darkMs = window_int(GetDlgItem(window, IDC_DARK), 150, 0);
    state.repeat = window_int(GetDlgItem(window, IDC_REPEAT), 1, 1);
    state.displayIndex = window_int(GetDlgItem(window, IDC_DISPLAY), 1, 0);

    if (state.images.empty()) {
        g_projectionRunning = false;
        set_status(L"지정 폴더에 지원되는 이미지가 없습니다.");
        return;
    }

    g_stopProjection = false;
    set_status(L"이미지 출력 중... (ESC 또는 Stop으로 종료)");
    std::thread(projection_thread, std::move(state)).detach();
}

HWND add_control(HWND parent, const wchar_t* cls, const wchar_t* text, DWORD style,
                 int x, int y, int width, int height, int id = 0) {
    return CreateWindowExW(
        0, cls, text, WS_CHILD | WS_VISIBLE | style,
        x, y, width, height, parent,
        reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),
        GetModuleHandleW(nullptr), nullptr);
}

LRESULT CALLBACK main_proc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    switch (message) {
    case WM_CREATE: {
        HFONT font = static_cast<HFONT>(GetStockObject(DEFAULT_GUI_FONT));
        auto setFont = [font](HWND control) {
            SendMessageW(control, WM_SETFONT, reinterpret_cast<WPARAM>(font), TRUE);
            return control;
        };

        setFont(add_control(window, L"STATIC", L"Blue LED", 0, 18, 20, 75, 24));
        g_slider = add_control(window, TRACKBAR_CLASSW, L"", TBS_HORZ | TBS_AUTOTICKS,
                               95, 15, 300, 35, IDC_BLUE_SLIDER);
        SendMessageW(g_slider, TBM_SETRANGE, TRUE, MAKELPARAM(0, 255));
        SendMessageW(g_slider, TBM_SETPOS, TRUE, 128);
        g_valueLabel = setFont(add_control(window, L"STATIC", L"128", SS_CENTER,
                                           405, 20, 45, 24, IDC_BLUE_VALUE));

        setFont(add_control(window, L"BUTTON", L"Apply LED", BS_PUSHBUTTON,
                            95, 55, 145, 32, IDC_APPLY));
        setFont(add_control(window, L"BUTTON", L"LED Off", BS_PUSHBUTTON,
                            250, 55, 145, 32, IDC_LED_OFF));

        setFont(add_control(window, L"STATIC", L"Pattern folder", 0, 18, 108, 95, 24));
        setFont(add_control(
            window,
            L"EDIT",
            path_join(get_exe_dir(), L"generated_patterns_centered").c_str(),
            WS_BORDER | ES_AUTOHSCROLL,
            115,
            105,
            335,
            25,
            IDC_FOLDER));

        setFont(add_control(window, L"STATIC", L"Exposure (ms)", 0, 18, 145, 95, 24));
        setFont(add_control(window, L"EDIT", L"1000", WS_BORDER | ES_NUMBER,
                            115, 142, 70, 25, IDC_EXPOSURE));
        setFont(add_control(window, L"STATIC", L"Dark (ms)", 0, 200, 145, 65, 24));
        setFont(add_control(window, L"EDIT", L"150", WS_BORDER | ES_NUMBER,
                            268, 142, 60, 25, IDC_DARK));
        setFont(add_control(window, L"STATIC", L"Repeat", 0, 340, 145, 48, 24));
        setFont(add_control(window, L"EDIT", L"1", WS_BORDER | ES_NUMBER,
                            390, 142, 60, 25, IDC_REPEAT));

        setFont(add_control(window, L"STATIC", L"Display index", 0, 18, 182, 95, 24));
        setFont(add_control(window, L"EDIT", L"1", WS_BORDER | ES_NUMBER,
                            115, 179, 70, 25, IDC_DISPLAY));
        setFont(add_control(window, L"BUTTON", L"Project images", BS_PUSHBUTTON,
                            200, 176, 125, 32, IDC_PROJECT));
        setFont(add_control(window, L"BUTTON", L"Stop", BS_PUSHBUTTON,
                            335, 176, 115, 32, IDC_STOP));

        g_statusLabel = setFont(add_control(window, L"STATIC",
            L"LightCrafter 4500을 USB로 연결한 뒤 사용하세요.", SS_LEFT,
            18, 225, 432, 38, IDC_STATUS));
        return 0;
    }
    case WM_HSCROLL:
        if (reinterpret_cast<HWND>(lParam) == g_slider) {
            const int value = static_cast<int>(SendMessageW(g_slider, TBM_GETPOS, 0, 0));
            SetWindowTextW(g_valueLabel, std::to_wstring(value).c_str());
        }
        return 0;
    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case IDC_APPLY: {
            const int value = static_cast<int>(SendMessageW(g_slider, TBM_GETPOS, 0, 0));
            std::wstring error;
            if (set_blue_led(value, error)) {
                set_status(L"Blue LED 밝기 " + std::to_wstring(value) + L" 적용 완료");
            } else {
                set_status(error);
            }
            return 0;
        }
        case IDC_LED_OFF: {
            SendMessageW(g_slider, TBM_SETPOS, TRUE, 0);
            SetWindowTextW(g_valueLabel, L"0");
            std::wstring error;
            set_status(set_blue_led(0, error) ? L"Blue LED 꺼짐" : error);
            return 0;
        }
        case IDC_PROJECT:
            start_projection(window);
            return 0;
        case IDC_STOP:
            g_stopProjection = true;
            set_status(L"이미지 출력을 중지하는 중...");
            return 0;
        }
        break;
    case WM_APP + 1:
        set_status(L"이미지 출력이 종료되었습니다.");
        return 0;
    case WM_DESTROY:
        g_stopProjection = true;
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, wParam, lParam);
}

} // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int showCommand) {
    INITCOMMONCONTROLSEX controls{sizeof(controls), ICC_BAR_CLASSES};
    InitCommonControlsEx(&controls);

    GdiplusStartupInput gdiplusInput;
    if (GdiplusStartup(&g_gdiplusToken, &gdiplusInput, nullptr) != Ok) {
        MessageBoxW(nullptr, L"GDI+ 초기화에 실패했습니다.", L"PRO4500", MB_ICONERROR);
        return 1;
    }

    WNDCLASSW wc{};
    wc.lpfnWndProc = main_proc;
    wc.hInstance = instance;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
    wc.lpszClassName = L"PRO4500ControlWindow";
    RegisterClassW(&wc);

    g_mainWindow = CreateWindowExW(
        0, wc.lpszClassName, L"PRO4500 Light Engine Control",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        CW_USEDEFAULT, CW_USEDEFAULT, 490, 310,
        nullptr, nullptr, instance, nullptr);

    if (!g_mainWindow) {
        GdiplusShutdown(g_gdiplusToken);
        return 1;
    }

    ShowWindow(g_mainWindow, showCommand);
    UpdateWindow(g_mainWindow);

    MSG message{};
    while (GetMessageW(&message, nullptr, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }

    GdiplusShutdown(g_gdiplusToken);
    return static_cast<int>(message.wParam);
}
